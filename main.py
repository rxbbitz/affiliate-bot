"""
AffiliateBot — main.py (v2 full auto)
─────────────────────────────────────
Flow:
  1. อ่าน config จาก Supabase (bot_config table)
  2. ดึง Datafeed ทั้งหมดจาก ACCESSTRADE TH (Shopee + TikTok)
  3. ส่ง batch สินค้าให้ Gemini คัดเลือก + เขียนแคปชัน ในคำสั่งเดียว
  4. Route ตาม Thread Routing table ใน Supabase
  5. ส่ง Discord Embed
  6. บันทึก send_log กลับ Supabase
"""

import os, asyncio, aiohttp, json, re, logging
from datetime import datetime, timezone

import discord
from discord.ext import tasks, commands
from supabase import create_client, Client
import google.generativeai as genai

from keep_alive import keep_alive

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Supabase ──────────────────────────────────────────────────────────────────
supa: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_KEY"],  # ใช้ service_role key
)

# ── Discord ───────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


# ════════════════════════════════════════════════════════════════════════════════
# SUPABASE HELPERS
# ════════════════════════════════════════════════════════════════════════════════

def get_config() -> dict:
    """โหลด config ทั้งหมดจากตาราง bot_config"""
    try:
        res = supa.table("bot_config").select("key,value").execute()
        return {r["key"]: r["value"] for r in (res.data or [])}
    except Exception as e:
        log.error(f"get_config: {e}")
        return {}


def get_active_routes() -> list[dict]:
    """โหลด thread routes ที่เปิดอยู่"""
    try:
        res = supa.table("thread_routes").select("*").eq("is_active", True).execute()
        return res.data or []
    except Exception as e:
        log.error(f"get_active_routes: {e}")
        return []


def was_sent(product_id: str, platform: str) -> bool:
    """เช็คว่าสินค้าชิ้นนี้เคยส่งแล้วหรือยัง"""
    try:
        res = (
            supa.table("send_log")
            .select("id")
            .eq("product_id", product_id)
            .eq("platform", platform)
            .limit(1)
            .execute()
        )
        return bool(res.data)
    except Exception as e:
        log.error(f"was_sent: {e}")
        return False


def upsert_products(products: list[dict]):
    """บันทึก/อัปเดตสินค้าลงตาราง products"""
    if not products:
        return
    try:
        supa.table("products").upsert(products, on_conflict="product_id,platform").execute()
    except Exception as e:
        log.error(f"upsert_products: {e}")


def write_log(product_id, name, platform, route_id, thread_id, msg_id, status):
    try:
        supa.table("send_log").insert({
            "product_id": product_id,
            "product_name": name,
            "platform": platform,
            "route_id": route_id,
            "discord_thread_id": thread_id,
            "discord_message_id": msg_id,
            "status": status,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        log.error(f"write_log: {e}")


# ════════════════════════════════════════════════════════════════════════════════
# ACCESSTRADE DATAFEED
# ════════════════════════════════════════════════════════════════════════════════

def build_datafeed_url(api_key: str, wid: str, campaign_id: str) -> str:
    """
    สร้าง Datafeed API URL ของ ACCESSTRADE TH
    รูปแบบ: https://member.accesstrade.in.th/datafeed/api/{campaign_id}?key={api_key}
    api_key = ตัวเลขที่อยู่ท้าย URL ของ Datafeed (เฉพาะของแต่ละ publisher)
    wid     = Website ID (สำหรับสร้าง affiliate link)
    """
    return (
        f"https://member.accesstrade.in.th/datafeed/api/{campaign_id}"
        f"?key={api_key}&wid={wid}&format=json"
    )


async def fetch_datafeed(
    session: aiohttp.ClientSession,
    api_key: str,
    wid: str,
    campaign_id: str,
    platform: str,
) -> list[dict]:
    """ดึง product datafeed จาก ACCESSTRADE แล้วแปลงเป็น list ของ product dict"""
    url = build_datafeed_url(api_key, wid, campaign_id)
    log.info(f"Fetching datafeed: {platform} campaign={campaign_id}")
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.error(f"Datafeed HTTP {resp.status} ({platform}): {body[:300]}")
                return []

            # รองรับทั้ง JSON array และ JSON object ที่มี key "data"/"products"
            raw = await resp.json(content_type=None)
            if isinstance(raw, dict):
                items = raw.get("data") or raw.get("products") or []
            elif isinstance(raw, list):
                items = raw
            else:
                log.warning(f"Unexpected datafeed format: {type(raw)}")
                return []

            products = []
            for item in items:
                pid = str(item.get("product_id") or item.get("id") or "")
                if not pid:
                    continue

                price_raw     = item.get("price") or item.get("sale_price") or 0
                original_raw  = item.get("original_price") or item.get("price_before_discount") or price_raw
                price         = float(str(price_raw).replace(",", "") or 0)
                original      = float(str(original_raw).replace(",", "") or price)
                discount_pct  = round((1 - price / original) * 100) if original > price > 0 else 0

                # affiliate link ที่ ACCESSTRADE สร้างให้ใน datafeed
                aff_url = (
                    item.get("affiliate_url")
                    or item.get("tracking_url")
                    or item.get("url", "")
                )

                products.append({
                    "product_id":     pid,
                    "platform":       platform,
                    "name":           str(item.get("name") or item.get("product_name") or pid)[:500],
                    "price":          price,
                    "discount_pct":   discount_pct,
                    "image_url":      str(item.get("image") or item.get("image_url") or "")[:1000],
                    "affiliate_url":  str(aff_url)[:1000],
                    "category_key":   str(item.get("category") or item.get("category_id") or "general").lower()[:100],
                    "category_label": str(item.get("category_name") or item.get("category") or "ทั่วไป")[:200],
                    "sold_count":     int(item.get("sold") or item.get("sold_count") or 0),
                    "rating":         float(item.get("rating") or 0),
                    "hot_score":      discount_pct,   # คำนวณเบื้องต้นจาก discount ก่อน AI ปรับ
                    "last_seen_at":   datetime.now(timezone.utc).isoformat(),
                })

            log.info(f"Fetched {len(products)} products ({platform})")
            return products

    except Exception as e:
        log.error(f"fetch_datafeed exception ({platform}): {e}")
        return []


# ════════════════════════════════════════════════════════════════════════════════
# GEMINI AI — คัดสินค้า + เขียนแคปชัน ในครั้งเดียว
# ════════════════════════════════════════════════════════════════════════════════

def ai_select_and_caption(
    products: list[dict],
    gemini_key: str,
    blocked_keywords: str,
    batch_size: int,
) -> list[dict]:
    """
    ส่ง product list ให้ Gemini:
      - เลือก batch_size สินค้าที่น่าสนใจที่สุดสำหรับ Discord affiliate
      - เขียนแคปชันภาษาไทยสั้นๆ กระตุ้นยอดซื้อ
      - คืนค่า list ของ {product_id, platform, caption, hot_score}
    """
    if not products or not gemini_key:
        return []

    genai.configure(api_key=gemini_key)
    model = genai.GenerativeModel("gemini-1.5-flash")

    blocked = [k.strip() for k in blocked_keywords.split(",") if k.strip()]

    # กรอง blocked keywords ก่อนส่ง AI (ประหยัด token)
    clean = [
        p for p in products
        if not any(kw.lower() in (p.get("name") or "").lower() for kw in blocked)
    ]

    if not clean:
        log.warning("No products left after blocked-keyword filter")
        return []

    # สร้าง mini-catalog สำหรับส่ง Gemini (ส่งแค่ field ที่จำเป็น ประหยัด token)
    catalog = [
        {
            "id":           p["product_id"],
            "platform":     p["platform"],
            "name":         p["name"],
            "price":        p["price"],
            "discount_pct": p["discount_pct"],
            "sold_count":   p.get("sold_count", 0),
            "rating":       p.get("rating", 0),
            "category":     p.get("category_label", ""),
        }
        for p in clean
    ]

    prompt = f"""คุณเป็นผู้เชี่ยวชาญด้าน affiliate marketing บน Discord

รายการสินค้าจาก ACCESSTRADE ด้านล่างนี้ (JSON array):
{json.dumps(catalog, ensure_ascii=False)}

งานของคุณ:
1. เลือก **{batch_size} สินค้า** ที่คาดว่าจะกระตุ้นการซื้อได้ดีที่สุดบน Discord
   - พิจารณา: ส่วนลดสูง, ราคาคุ้มค่า, ยอดขายดี, rating ดี, ความหลากหลายของหมวดหมู่
   - ห้ามเลือกสินค้าที่มีชื่อคลุมเครือหรือไม่น่าเชื่อถือ
2. เขียน **แคปชันภาษาไทย 1-2 ประโยค** สำหรับแต่ละสินค้า ให้กระชับ น่าคลิก มี emoji 1-2 ตัว
3. ให้ **hot_score 0-100** สำหรับแต่ละสินค้า (100 = น่าซื้อที่สุด)

ตอบเป็น **JSON array เท่านั้น** ไม่มี markdown ไม่มี backtick ไม่มีคำอธิบายนำหน้า:
[
  {{"id": "product_id", "platform": "shopee|tiktok", "caption": "แคปชัน...", "hot_score": 85}},
  ...
]"""

    try:
        response = model.generate_content(prompt)
        text = response.text.strip()

        # กัน Gemini ใส่ markdown code block มา
        text = re.sub(r"```(?:json)?", "", text).strip()

        selected = json.loads(text)
        log.info(f"Gemini selected {len(selected)} products")
        return selected

    except json.JSONDecodeError as e:
        log.error(f"Gemini JSON parse error: {e}\nRaw: {text[:500]}")
        return []
    except Exception as e:
        log.error(f"ai_select_and_caption error: {e}")
        return []


# ════════════════════════════════════════════════════════════════════════════════
# ROUTING
# ════════════════════════════════════════════════════════════════════════════════

def resolve_route(routes: list[dict], platform: str, category_key: str | None) -> dict | None:
    """exact match → platform wildcard (*) → None"""
    for r in routes:
        if r["platform"] == platform and r["category_key"] == category_key:
            return r
    for r in routes:
        if r["platform"] == platform and r["category_key"] == "*":
            return r
    return None


# ════════════════════════════════════════════════════════════════════════════════
# DISCORD EMBED
# ════════════════════════════════════════════════════════════════════════════════

PLATFORM_COLOR = {"shopee": 0xEE4D2D, "tiktok": 0x010101}
PLATFORM_LABEL = {"shopee": "🛍️ Shopee", "tiktok": "🎵 TikTok Shop"}

def build_embed(product: dict, caption: str, hot_score: int) -> discord.Embed:
    plat  = product.get("platform", "shopee")
    label = PLATFORM_LABEL.get(plat, plat.title())
    color = PLATFORM_COLOR.get(plat, 0xf97316)

    embed = discord.Embed(
        title=product["name"][:256],
        url=product.get("affiliate_url") or None,
        description=caption or "สินค้าน่าสนใจ คลิกดูเลย!",
        color=color,
    )
    embed.add_field(name="💰 ราคา",     value=f"฿{product['price']:,.0f}",      inline=True)
    embed.add_field(name="🔥 ส่วนลด",  value=f"{product['discount_pct']}%",     inline=True)
    embed.add_field(name="⭐ Hot Score", value=f"{hot_score}/100",               inline=True)

    if product.get("affiliate_url"):
        embed.add_field(
            name="🔗 ลิงก์",
            value=f"[👉 คลิกซื้อเลย!]({product['affiliate_url']})",
            inline=False,
        )
    if product.get("image_url"):
        embed.set_thumbnail(url=product["image_url"])

    embed.set_footer(text=f"{label} • AffiliateBot")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


# ════════════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ════════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    log.info(f"✅ Bot ready: {bot.user} (id={bot.user.id})")
    if not send_affiliate_products.is_running():
        send_affiliate_products.start()


@tasks.loop(hours=6)
async def send_affiliate_products():
    log.info("═══ Starting affiliate cycle ═══")

    # ── 1. โหลด config ──────────────────────────────────────────────────────
    cfg = get_config()
    if not cfg:
        log.warning("bot_config table is empty — skipping")
        return

    at_key       = cfg.get("accesstrade_api_key", "").strip()
    wid          = cfg.get("accesstrade_wid", "").strip()
    shopee_cid   = cfg.get("shopee_campaign_id", "").strip()
    tiktok_cid   = cfg.get("tiktok_campaign_id", "").strip()
    gemini_key   = cfg.get("gemini_api_key", "").strip()
    fallback_id  = cfg.get("fallback_channel_id", "").strip()
    batch_size   = int(cfg.get("batch_size", 5))
    blocked_kw   = cfg.get("blocked_keywords", "")

    if not all([at_key, wid, gemini_key, fallback_id]):
        log.warning("Missing required config keys — skipping")
        return

    routes = get_active_routes()
    log.info(f"Active routes: {len(routes)}")

    # ── 2. ดึง Datafeed ──────────────────────────────────────────────────────
    all_products: list[dict] = []
    async with aiohttp.ClientSession() as session:
        if shopee_cid:
            items = await fetch_datafeed(session, at_key, wid, shopee_cid, "shopee")
            all_products.extend(items)
        if tiktok_cid:
            items = await fetch_datafeed(session, at_key, wid, tiktok_cid, "tiktok")
            all_products.extend(items)

    if not all_products:
        log.warning("No products from ACCESSTRADE — skipping cycle")
        return

    # บันทึกสินค้าทั้งหมดลง DB (ไม่ว่าจะส่งหรือไม่)
    upsert_products(all_products)

    # ── 3. กรองสินค้าที่เคยส่งแล้วออก ───────────────────────────────────────
    unsent = [p for p in all_products if not was_sent(p["product_id"], p["platform"])]
    log.info(f"Unsent products: {len(unsent)} / {len(all_products)}")

    if not unsent:
        log.info("All products already sent — nothing to do")
        return

    # ── 4. ให้ Gemini คัดเลือก + เขียนแคปชัน ────────────────────────────────
    # ส่ง unsent ทั้งหมดให้ Gemini เลือก batch_size ชิ้น
    selected = ai_select_and_caption(unsent, gemini_key, blocked_kw, batch_size)

    if not selected:
        log.warning("Gemini returned no selections — skipping")
        return

    # map กลับเป็น product dict เต็มๆ
    product_map = {(p["product_id"], p["platform"]): p for p in unsent}

    # ── 5. ส่ง Discord ────────────────────────────────────────────────────────
    sent_count = 0
    for sel in selected:
        pid      = str(sel.get("id", ""))
        platform = str(sel.get("platform", ""))
        caption  = str(sel.get("caption", ""))
        score    = int(sel.get("hot_score", 50))

        product = product_map.get((pid, platform))
        if not product:
            log.warning(f"Gemini picked unknown product id={pid} platform={platform}")
            continue

        # หา target channel/thread
        route     = resolve_route(routes, platform, product.get("category_key"))
        target_id = int(route["thread_id"]) if route else int(fallback_id)

        channel = bot.get_channel(target_id)
        if channel is None:
            log.warning(f"Channel {target_id} not cached — trying fallback {fallback_id}")
            channel = bot.get_channel(int(fallback_id))

        if channel is None:
            log.error(f"Cannot find channel {target_id} or fallback — skip product {pid}")
            write_log(pid, product["name"], platform, None, None, None, "failed")
            continue

        try:
            # อัปเดต hot_score จาก AI ก่อนบันทึก
            product["hot_score"] = score
            embed = build_embed(product, caption, score)
            msg   = await channel.send(embed=embed)

            status = "sent" if route else "fallback"
            write_log(
                pid, product["name"], platform,
                route["id"] if route else None,
                str(target_id), str(msg.id), status,
            )
            sent_count += 1
            log.info(f"✅ Sent '{product['name'][:40]}' → {target_id} [{status}]")

        except discord.HTTPException as e:
            log.error(f"Discord error for {pid}: {e}")
            write_log(pid, product["name"], platform, None, None, None, "failed")
        except Exception as e:
            log.error(f"Unexpected error for {pid}: {e}")

        await asyncio.sleep(2)  # ป้องกัน Discord rate limit

    log.info(f"═══ Cycle done — sent {sent_count}/{len(selected)} products ═══")


@send_affiliate_products.before_loop
async def before_loop():
    await bot.wait_until_ready()


# ── Run ───────────────────────────────────────────────────────────────────────
keep_alive()
bot.run(os.environ["DISCORD_BOT_TOKEN"])
