import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- 環境変数の読み込み ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
SPREADSHEET_ID = os.getenv("GOOGLE_SHEET_ID")
CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH")

# --- Discord設定 ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

# --- Google Sheets接続 ---
def get_sheets_service():
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_PATH,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    service = build("sheets", "v4", credentials=creds)
    return service.spreadsheets()

# --- Botイベント ---
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

# --- コマンド ---
@bot.command()
async def ping(ctx):
    await ctx.send("🏓 Pong!")

@bot.command()
async def reserve(ctx, name: str, time: str):
    sheet = get_sheets_service()
    values = [[ctx.author.name, name, time]]

    try:
        sheet.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="sheet1",
            valueInputOption="USER_ENTERED",
            body={"values": values}
        ).execute()
        await ctx.send(f"✅ {name} の予約を {time} に登録しました！")
    except Exception as e:
        await ctx.send(f"❌ エラーが発生しました: {e}")
        print(e)

@bot.command()
async def list(ctx):
    """Google Sheets から予約一覧を表示"""
    sheet = get_sheets_service()
    result = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="sheet1"  # 実際のシート名に合わせて変更
    ).execute()

    values = result.get("values", [])

    if not values:
        await ctx.send("📭 現在、予約はありません。")
        return

    msg = "📋 **予約一覧**\n"
    for row in values:
        if len(row) >= 3:
            user, name, time = row
            msg += f"- {user} さん：{name}（{time}）\n"

    await ctx.send(msg)

# --- 起動 ---
bot.run(TOKEN)
