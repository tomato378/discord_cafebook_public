import os
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- 環境変数 ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
SPREADSHEET_ID = os.getenv("GOOGLE_SHEET_ID")
CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH")
GUILD_ID = int(os.getenv("GUILD_ID"))

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
    return build("sheets", "v4", credentials=creds).spreadsheets()


# --- モーダル定義 ---
class ReservationModal(discord.ui.Modal, title="☕ カフェ予約フォーム"):
    user_name = discord.ui.TextInput(label="予約者ネーム", placeholder="例：トマト", required=True)
    menu_name = discord.ui.TextInput(label="メニュー名", placeholder="例：カフェラテ", required=True)
    time = discord.ui.TextInput(label="予約時間", placeholder="例：13:30", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        sheet = get_sheets_service()
        values = [[
            interaction.user.name,
            self.user_name.value,
            self.menu_name.value,
            self.time.value
        ]]

        try:
            sheet.values().append(
                spreadsheetId=SPREADSHEET_ID,
                range="Sheet1!A:D",
                valueInputOption="USER_ENTERED",
                body={"values": values}
            ).execute()
            await interaction.response.send_message(
                f"✅ 予約を登録しました！\n"
                f"- 予約者ネーム：{self.user_name.value}\n"
                f"- メニュー：{self.menu_name.value}\n"
                f"- 時間：{self.time.value}",
                ephemeral=True  # ユーザーにだけ表示
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ エラーが発生しました: {e}", ephemeral=True
            )


# --- Botイベント ---
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    try:
        guild = discord.Object(id=GUILD_ID)
        synced = await bot.tree.sync(guild=guild)
        print(f"🔁 Slash commands synced to guild ({len(synced)} commands)")
    except Exception as e:
        print(f"⚠️ Sync failed: {e}")



# --- Slashコマンド ---
@bot.tree.command(name="ping", description="Pong! を返します")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("🏓 Pong!")


@bot.tree.command(name="reserve_form", description="予約フォームを開きます")
async def reserve_form(interaction: discord.Interaction):
    """モーダルで予約フォームを開く"""
    modal = ReservationModal()
    await interaction.response.send_modal(modal)


@bot.tree.command(name="list", description="予約一覧を表示します")
async def list_reservations(interaction: discord.Interaction):
    sheet = get_sheets_service()
    result = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="sheet1"
    ).execute()
    values = result.get("values", [])

    if not values:
        await interaction.response.send_message("📭 現在、予約はありません。")
        return

    msg = "📋 **予約一覧**\n"
    for row in values:
        if len(row) >= 4:
            user, reserver_name, menu, time = row
            msg += f"- {reserver_name} さん（by {user}）：{menu}（{time}）\n"

    await interaction.response.send_message(msg)


# --- 起動 ---
bot.run(TOKEN)
