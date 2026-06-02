import os
import discord
from discord.ext import tasks, commands
from supabase import create_client, Client
from keep_alive import keep_alive

# 1. ตั้งค่าบอท Discord
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# 2. ตั้งค่า Supabase (ดึงข้อมูลมาจากระบบของ Render)
url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(url, key)

@bot.event
async def on_ready():
    print(f'✅ ล็อกอินบอทสำเร็จในชื่อ {bot.user}')
    send_affiliate_products.start() # สั่งให้ระบบส่งอัตโนมัติเริ่มทำงาน

# 3. ระบบส่งสินค้าอัตโนมัติ (ตั้งไว้ให้รันทุกๆ 6 ชั่วโมง)
@tasks.loop(hours=6)
async def send_affiliate_products():
    print("⏳ กำลังเริ่มดึงข้อมูลและส่งสินค้า...")
    
    # ---------------------------------------------------------
    # พื้นที่สำหรับใส่โค้ด ACCESSTRADE และ Gemini AI ในอนาคต
    # ---------------------------------------------------------

    # ตัวอย่างการส่งข้อความ (ดึง Channel ID จากการตั้งค่า)
    fallback_channel_id = int(os.environ.get("DISCORD_FALLBACK_CHANNEL_ID"))
    channel = bot.get_channel(fallback_channel_id)
    
    if channel:
        # สร้างการ์ดข้อความสวยๆ (Embed)
        embed = discord.Embed(
            title="🔥 สินค้าขายดีมาใหม่!", 
            description="ข้อความวิเคราะห์จาก AI จะอยู่ตรงนี้", 
            color=0xf97316 # สีส้ม
        )
        embed.add_field(name="ราคา", value="฿99", inline=True)
        embed.add_field(name="ส่วนลด", value="50%", inline=True)
        embed.set_footer(text="#Shopee #Affiliate")
        
        await channel.send(embed=embed)
        print("✅ ส่งข้อความเข้า Discord สำเร็จ!")

# 4. รันตัวกันบอทหลับ (ไฟล์ keep_alive)
keep_alive()

# 5. รันบอท Discord
bot_token = os.environ.get("DISCORD_BOT_TOKEN")
bot.run(bot_token)