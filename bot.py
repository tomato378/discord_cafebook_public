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
async def reserve(ctx, reserver: str, name: str, time: str):
    """予約を登録"""
    sheet = get_sheets_service()
    values = [[reserver, name, time, ctx.author.name]]

    try:
        sheet.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Sheet1",
            valueInputOption="USER_ENTERED",
            body={"values": values}
        ).execute()
        await ctx.send(f"✅ 予約者「{reserver}」として {name}（{time}）を登録しました！")
    except Exception as e:
        await ctx.send(f"❌ エラーが発生しました: {e}")
        print(e)

@bot.command()
async def cancel(ctx, reserver: str, time: str):
    """予約者名と時間でキャンセル"""
    sheet = get_sheets_service()
    result = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Sheet1"
    ).execute()

    values = result.get("values", [])
    if not values:
        await ctx.send("📭 現在、予約はありません。")
        return

    # 行を検索
    target_index = None
    for i, row in enumerate(values):
        # [予約者名, 内容, 時間, Discordユーザー]
        if len(row) >= 3 and row[0] == reserver and row[2] == time:
            target_index = i + 1
            break

    if target_index is None:
        await ctx.send(f"❌ 予約者「{reserver}」の {time} の予約は見つかりませんでした。")
        return

    # 削除処理
    sheet.values().clear(
        spreadsheetId=SPREADSHEET_ID,
        range=f"Sheet1!A{target_index}:D{target_index}"
    ).execute()

    await ctx.send(f"🗑️ 予約者「{reserver}」の {time} の予約をキャンセルしました。")

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
            reserver = row[0]
            menu = row[1]
            time = row[2]
            msg += f"- 予約者：{reserver}｜メニュー：{menu}｜時間：{time}\n"

    await ctx.send(msg)

# --- 起動 ---
bot.run(TOKEN)
