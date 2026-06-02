import os
import asyncio
import aiohttp
import discord
from discord.ext import tasks, commands
from supabase import create_client, Client
from keep_alive import keep_alive
import google.generativeai as genai
from datetime import datetime, timezone
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── Supabase ───────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ─── Discord ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ─── helpers ────────────────────────────────────────────────────────────────

def get_config() -> dict:
    """อ่านค่า config จากตาราง bot_config ใน Supabase"""
    try:
        res = supabase.table("bot_config").select("key,value").execute()
        return {row["key"]: row["value"] for row in res.data}
    except Exception as e:
        log.error(f"get_config error: {e}")
        return {}


def get_thread_routes() -> list[dict]:
    """อ่าน thread routes ที่ active อยู่จาก Supabase"""
    try:
        res = (
            supabase.table("thread_routes")
            .select("*")
            .eq("is_active", True)
            .execute()
        )
        return res.data or []
    except Exception as e:
        log.error(f"get_thread_routes error: {e}")
        return []


def already_sent(product_id: str) -> bool:
    """เช็คว่าสินค้านี้เคยส่งไปแล้วหรือยัง"""
    try:
        res = (
            supabase.table("send_log")
            .select("id")
            .eq("product_id", product_id)
            .limit(1)
            .execute()
        )
        return len(res.data) > 0
    except Exception as e:
        log.error(f"already_sent check error: {e}")
        return False


def log_sent(product_id: str, product_name: str, platform: str,
             route_id: str | None, thread_id: str | None,
             discord_msg_id: str | None, status: str):
    """บันทึก log การส่งกลับไปที่ Supabase"""
    try:
        supabase.table("send_log").insert({
            "product_id": product_id,
            "product_name": product_name,
            "platform": platform,
            "route_id": route_id,
            "discord_thread_id": thread_id,
            "discord_message_id": discord_msg_id,
            "status": status,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        log.error(f"log_sent error: {e}")


def upsert_product(p: dict):
    """บันทึก/อัปเดตข้อมูลสินค้าลงตาราง products"""
    try:
        supabase.table("products").upsert(p, on_conflict="product_id,platform").execute()
    except Exception as e:
        log.error(f"upsert_product error: {e}")


def resolve_route(routes: list[dict], platform: str, category_key: str | None) -> dict | None:
    """หา route ที่ตรงกับ platform + category (exact match ก่อน แล้วค่อย wildcard *)"""
    # exact match
    for r in routes:
        if r["platform"] == platform and r["category_key"] == category_key:
            return r
    # wildcard
    for r in routes:
        if r["platform"] == platform and r["category_key"] == "*":
            return r
    return None


# ─── ACCESSTRADE ─────────────────────────────────────────────────────────────

ACCESSTRADE_BASE = "https://api.accesstrade.in.th/v1"

async def fetch_accesstrade_products(session: aiohttp.ClientSession,
                                     api_key: str,
                                     wid: str,
                                     campaign_id: str,
                                     platform: str,
                                     limit: int = 20) -> list[dict]:
    """
    ดึงสินค้า Top Products จาก ACCESSTRADE API
    endpoint: GET /products?campaign_id=...&limit=...
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {
        "campaign_id": campaign_id,
        "limit": limit,
        "sort": "hot_score",
        "order": "desc",
    }
    try:
        async with session.get(
            f"{ACCESSTRADE_BASE}/products",
            headers=headers,
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                log.error(f"ACCESSTRADE error {resp.status}: {text[:200]}")
                return []
            data = await resp.json()
            items = data.get("data", data) if isinstance(data, dict) else data
            products = []
            for item in items:
                pid = str(item.get("product_id") or item.get("id") or "")
                if not pid:
                    continue
                # สร้าง affiliate link ด้วย WID
                raw_url = item.get("url") or item.get("product_url") or ""
                affiliate_url = f"https://www.accesstrade.in.th/l?wid={wid}&url={raw_url}" if raw_url else ""

                price = float(item.get("price") or item.get("sale_price") or 0)
                original = float(item.get("original_price") or item.get("price") or price)
                discount_pct = round((1 - price / original) * 100) if original > 0 else 0

                products.append({
                    "product_id": pid,
                    "platform": platform,
                    "name": item.get("name") or item.get("product_name") or pid,
                    "price": price,
                    "discount_pct": discount_pct,
                    "image_url": item.get("image") or item.get("image_url") or "",
                    "affiliate_url": affiliate_url,
                    "category_key": str(item.get("category") or item.get("category_id") or "general").lower(),
                    "category_label": str(item.get("category_name") or item.get("category") or "ทั่วไป"),
                    "sold_count": int(item.get("sold") or item.get("sold_count") or 0),
                    "rating": float(item.get("rating") or 0),
                    "hot_score": int(item.get("hot_score") or discount_pct),
                    "last_seen_at": datetime.now(timezone.utc).isoformat(),
                })
            return products
    except Exception as e:
        log.error(f"fetch_accesstrade_products exception: {e}")
        return []


# ─── Gemini AI ───────────────────────────────────────────────────────────────

def generate_caption(product: dict, gemini_key: str, blocked_keywords: str = "") -> str:
    """
    ให้ Gemini เขียนแคปชันภาษาไทยสั้นๆ กระตุ้นยอดขาย
    คืนค่า caption string หรือ "" ถ้า error
    """
    try:
        genai.configure(api_key=gemini_key)
        model = genai.GenerativeModel("gemini-1.5-flash")

        blocked = [kw.strip() for kw in blocked_keywords.split(",") if kw.strip()]
        block_note = f"\nห้ามใช้คำเหล่านี้: {', '.join(blocked)}" if blocked else ""

        prompt = f"""คุณเป็นนักการตลาด affiliate ที่เขียนแคปชันสินค้าภาษาไทย
เขียนแคปชันสั้นๆ 1-2 ประโยค กระตุ้นการซื้อ ใช้ emoji 1-2 ตัว
ห้ามโกหกหรือพูดเกินจริง ต้องอ้างอิงจากข้อมูลสินค้าที่ให้{block_note}

ชื่อสินค้า: {product['name']}
ราคา: ฿{product['price']:,.0f}
ส่วนลด: {product['discount_pct']}%
หมวดหมู่: {product.get('category_label', '')}

ตอบเป็นแคปชันสั้นๆ อย่างเดียว ไม่ต้องมีคำอธิบายหรือ prefix"""

        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        log.error(f"Gemini error: {e}")
        return ""


def filter_by_ai(product: dict, gemini_key: str, blocked_keywords: str) -> bool:
    """ให้ Gemini ตรวจสอบว่าสินค้าเหมาะสมหรือไม่ (คืน True = ผ่าน)"""
    try:
        genai.configure(api_key=gemini_key)
        model = genai.GenerativeModel("gemini-1.5-flash")
        blocked = [kw.strip() for kw in blocked_keywords.split(",") if kw.strip()]

        # เช็ค blocked keywords ก่อนโดยไม่ต้องเรียก AI
        name_lower = product["name"].lower()
        for kw in blocked:
            if kw.lower() in name_lower:
                log.info(f"Blocked keyword '{kw}' found in: {product['name']}")
                return False

        prompt = f"""ตรวจสอบสินค้าชิ้นนี้ว่าเหมาะสมกับการโฆษณา affiliate บน Discord หรือไม่
ชื่อสินค้า: {product['name']}
ตอบเป็น JSON เท่านั้น: {{"pass": true}} หรือ {{"pass": false, "reason": "เหตุผล"}}"""

        response = model.generate_content(prompt)
        import json, re
        text = response.text.strip()
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            result = json.loads(match.group())
            if not result.get("pass", True):
                log.info(f"AI filtered out: {product['name']} — {result.get('reason','')}")
                return False
        return True
    except Exception as e:
        log.error(f"filter_by_ai error: {e}")
        return True  # ถ้า error ให้ผ่านไปก่อน


# ─── Discord Embed ────────────────────────────────────────────────────────────

def build_embed(product: dict, caption: str) -> discord.Embed:
    """สร้าง Discord Embed สำหรับสินค้า"""
    platform_emoji = "🛍️" if product["platform"] == "shopee" else "🎵"
    platform_label = "Shopee" if product["platform"] == "shopee" else "TikTok Shop"

    embed = discord.Embed(
        title=product["name"][:256],
        url=product.get("affiliate_url") or discord.Embed.Empty,
        description=caption or "สินค้าน่าสนใจ มาดูกันเลย!",
        color=0xEE4D2D if product["platform"] == "shopee" else 0x000000,
    )

    embed.add_field(name="💰 ราคา", value=f"฿{product['price']:,.0f}", inline=True)
    embed.add_field(name="🔥 ส่วนลด", value=f"{product['discount_pct']}%", inline=True)
    embed.add_field(name="⭐ Hot Score", value=str(product.get("hot_score", 0)), inline=True)

    if product.get("affiliate_url"):
        embed.add_field(name="🔗 ลิงก์", value=f"[คลิกซื้อเลย!]({product['affiliate_url']})", inline=False)

    if product.get("image_url"):
        embed.set_thumbnail(url=product["image_url"])

    embed.set_footer(text=f"{platform_emoji} {platform_label} • AffiliateBot")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


# ─── Main Loop ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    log.info(f"✅ Logged in as {bot.user} (id={bot.user.id})")
    if not send_affiliate_products.is_running():
        send_affiliate_products.start()


@tasks.loop(hours=6)
async def send_affiliate_products():
    log.info("⏳ Starting affiliate product cycle...")

    cfg = get_config()
    if not cfg:
        log.warning("No config found in bot_config table — skipping cycle")
        return

    # อ่าน config
    at_key        = cfg.get("accesstrade_api_key", "")
    wid           = cfg.get("accesstrade_wid", "")
    shopee_cid    = cfg.get("shopee_campaign_id", "")
    tiktok_cid    = cfg.get("tiktok_campaign_id", "")
    gemini_key    = cfg.get("gemini_api_key", "")
    fallback_id   = cfg.get("fallback_channel_id", "")
    batch_size    = int(cfg.get("batch_size", 5))
    min_hot_score = int(cfg.get("min_hot_score", 60))
    ai_filter_on  = cfg.get("ai_filter", "true").lower() == "true"
    blocked_kw    = cfg.get("blocked_keywords", "")

    if not all([at_key, wid, gemini_key, fallback_id]):
        log.warning("Missing required config (accesstrade_api_key, accesstrade_wid, gemini_api_key, fallback_channel_id)")
        return

    routes = get_thread_routes()
    log.info(f"Loaded {len(routes)} active routes")

    # ─── ดึงสินค้าจาก ACCESSTRADE ───
    all_products = []
    async with aiohttp.ClientSession() as session:
        if shopee_cid:
            items = await fetch_accesstrade_products(session, at_key, wid, shopee_cid, "shopee", limit=30)
            all_products.extend(items)
            log.info(f"Fetched {len(items)} Shopee products")
        if tiktok_cid:
            items = await fetch_accesstrade_products(session, at_key, wid, tiktok_cid, "tiktok", limit=30)
            all_products.extend(items)
            log.info(f"Fetched {len(items)} TikTok products")

    if not all_products:
        log.warning("No products fetched from ACCESSTRADE")
        return

    # ─── Filter + score ───
    # กรอง hot_score
    filtered = [p for p in all_products if p.get("hot_score", 0) >= min_hot_score]
    # กรองที่เคยส่งแล้ว
    filtered = [p for p in filtered if not already_sent(p["product_id"])]
    # เรียงตาม hot_score
    filtered.sort(key=lambda x: x.get("hot_score", 0), reverse=True)
    # จำกัดจำนวน
    batch = filtered[:batch_size]
    log.info(f"After filters: {len(batch)} products to send (from {len(all_products)} total)")

    # ─── Upsert ข้อมูลสินค้าทั้งหมดลง DB ───
    for p in all_products:
        upsert_product(p)

    # ─── ส่งทีละชิ้น ───
    sent_count = 0
    for product in batch:
        try:
            # AI filter
            if ai_filter_on and gemini_key:
                if not filter_by_ai(product, gemini_key, blocked_kw):
                    continue

            # สร้าง caption
            caption = ""
            if gemini_key:
                caption = generate_caption(product, gemini_key, blocked_kw)

            # หา route
            route = resolve_route(routes, product["platform"], product.get("category_key"))
            target_id = int(route["thread_id"]) if route else int(fallback_id)
            channel = bot.get_channel(target_id)

            if channel is None:
                log.warning(f"Channel/Thread {target_id} not found — trying fallback")
                target_id = int(fallback_id)
                channel = bot.get_channel(target_id)

            if channel is None:
                log.error(f"Fallback channel {fallback_id} not found either — skip")
                log_sent(product["product_id"], product["name"], product["platform"],
                         None, None, None, "failed")
                continue

            embed = build_embed(product, caption)
            msg = await channel.send(embed=embed)

            status = "sent" if route else "fallback"
            log_sent(
                product["product_id"], product["name"], product["platform"],
                route["id"] if route else None,
                str(target_id), str(msg.id), status
            )
            sent_count += 1
            log.info(f"✅ Sent '{product['name'][:40]}' → channel {target_id}")

            # หน่วงเวลาเล็กน้อยเพื่อไม่ให้ Discord rate limit
            await asyncio.sleep(2)

        except discord.HTTPException as e:
            log.error(f"Discord HTTP error for {product['product_id']}: {e}")
            log_sent(product["product_id"], product["name"], product["platform"],
                     None, None, None, "failed")
        except Exception as e:
            log.error(f"Unexpected error for {product['product_id']}: {e}")

    log.info(f"✅ Cycle complete — sent {sent_count} products")


@send_affiliate_products.before_loop
async def before_loop():
    await bot.wait_until_ready()


# ─── Run ──────────────────────────────────────────────────────────────────────
keep_alive()
bot.run(os.environ["DISCORD_BOT_TOKEN"])
