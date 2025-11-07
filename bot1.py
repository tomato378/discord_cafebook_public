import os
import discord
from discord.ext import commands
from discord import app_commands, ui
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- 環境変数読み込み ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
SPREADSHEET_ID = os.getenv("GOOGLE_SHEET_ID")
CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH")

# --- Discord Bot設定 ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

# --- Google Sheets 接続 ---
def get_sheets_service():
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_PATH,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    service = build("sheets", "v4", credentials=creds)
    return service.spreadsheets()

# --- モーダル定義 ---
class ReservationModal(ui.Modal, title="☕ 予約情報を入力してください"):
    def __init__(self, channel_name: str):
        super().__init__()
        self.channel_name = channel_name

        self.user_name = ui.TextInput(label="予約者名", placeholder="キャンセルの際に必要です")
        self.day = ui.TextInput(label="予約日", default="2025/11/01", placeholder="例: 2025/11/01")
        self.start_time = ui.TextInput(label="開始時間", placeholder="例: 13:00(半角)")
        self.end_time = ui.TextInput(label="終了時間", placeholder="例: 14:00( 半角)")

        self.add_item(self.user_name)
        self.add_item(self.start_time)
        self.add_item(self.end_time)
        self.add_item(self.day)

    async def on_submit(self, interaction: discord.Interaction):
        sheet = get_sheets_service()
        values = [[
            self.user_name.value,
            self.channel_name,
            self.day.value,
            self.start_time.value,
            self.end_time.value
        ]]


        try:
            sheet.values().append(
                spreadsheetId=SPREADSHEET_ID,
                range="sheet1!A:E",
                valueInputOption="USER_ENTERED",
                body={"values": values}
            ).execute()
            await interaction.response.send_message(
                f"✅ {self.user_name.value} さんの予約を登録しました！\n"
                f"🧾 {self.channel_name}チャンネル\n"
                f"🕒 時間：{self.start_time.value}~{self.end_time.value}",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ エラーが発生しました: {e}", ephemeral=True
            )

# --- プルダウンメニュー定義 ---
class MenuSelect(ui.Select):
    def __init__(self, category_channels):
        options = [
            discord.SelectOption(
                label=ch.name,
                description=f"{'ボイスチャンネル' if isinstance(ch, discord.VoiceChannel) else 'テキストチャンネル'} を予約"
            )
            for ch in category_channels
            if isinstance(ch, (discord.TextChannel, discord.VoiceChannel))
        ]
        super().__init__(
            placeholder="チャンネルを選択してください ☕",
            options=options,
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        channel_name = self.values[0]
        modal = ReservationModal(channel_name)
        await interaction.response.send_modal(modal)

# --- View定義 ---
class MenuSelectView(ui.View):
    def __init__(self, category_channels):
        super().__init__(timeout=60)
        self.add_item(MenuSelect(category_channels))

# --- コマンド定義 ---
@bot.tree.command(name="reserve_form", description="ポップアップで予約を登録します")
async def reserve_form(interaction: discord.Interaction):
    category = discord.utils.get(interaction.guild.categories, name="カフェ")

    
    if not category:
        await interaction.response.send_message("❌ 『カフェ』カテゴリーが見つかりません。", ephemeral=True)
        return

    view = MenuSelectView(category.channels)
    await interaction.response.send_message("☕ メニューを選んでください：", view=view, ephemeral=True)

# --- Bot起動 ---
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"🔁 Slash commands synced globally ({len(synced)} commands)")
    except Exception as e:
        print(f"⚠️ Sync failed: {e}")

bot.run(TOKEN)
