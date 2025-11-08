import os
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- 環境変数読み込み ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
SPREADSHEET_ID = os.getenv("GOOGLE_SHEET_ID")
CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH")
GUILD_ID = int(os.getenv("GUILD_ID"))


# --- Discord Bot設定 ---
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="/", intents=intents)

# --- Google Sheets 接続 ---
def get_sheets_service():
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_PATH,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    service = build("sheets", "v4", credentials=creds)
    return service.spreadsheets()

# --- テストコマンド ---
@bot.tree.command(name="sheet_test", description="スプレッドシートの内容を確認します")
async def sheet_test(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        sheet = get_sheets_service()
        result = sheet.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="sheet1!A:E"
        ).execute()

        values = result.get("values", [])
        if not values:
            await interaction.followup.send("📭 シートは空です。", ephemeral=True)
            return

        # 先頭5行だけを表示
        content = "\n".join([", ".join(row) for row in values[:5]])
        await interaction.followup.send(f"🧾 スプレッドシートの内容:\n```\n{content}\n```", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)

# --- 起動 ---
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"🔁 Slash commands synced ({len(synced)} commands to guild {GUILD_ID})")
    except Exception as e:
        print(f"⚠️ Sync failed: {e}")

bot.run(TOKEN)
