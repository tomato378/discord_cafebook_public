# --- 修正版（① 最低限動くための修正）---
# 主な修正点:
# 1. delete_row の index を 0-index に統一
# 2. header チェックを安全化（既にデータがある時に2重追加を防ぐ）
# 3. category ID の扱いを一本化

import os
import discord
from discord.ext import commands
from discord import app_commands, ui
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime

# --- 環境変数 ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
SPREADSHEET_ID = os.getenv("GOOGLE_SHEET_ID")
CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH")
CAFE_CATEGORY_ID = int(os.getenv("CAFE_CATEGORY_ID_TEST", "0"))

guild_id_env = os.getenv("GUILD_ID_TEST")
GUILD_OBJ = discord.Object(id=int(guild_id_env)) if guild_id_env else None

# --- Discord Bot 設定 ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

# --- Google Sheet 操作 ---
class SheetOperations:
    def __init__(self):
        self.service = None
        self.sheet_name = "sheet1"
        self.header = ["ユーザー名", "メニュー名", "日付", "開始", "終了"]

    def get_service(self):
        if not self.service:
            creds = service_account.Credentials.from_service_account_file(
                CREDENTIALS_PATH,
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
            self.service = build("sheets", "v4", credentials=creds).spreadsheets()
        return self.service

    def get_values(self):
        service = self.get_service()
        result = service.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{self.sheet_name}!A:E"
        ).execute()

        rows = result.get("values", [])

        # --- 修正: ヘッダー強制追加ではなく、「無ければ追加」に変更 ---
        if not rows:
            self.append_row(self.header)
            return []
        if rows[0] != self.header:
            rows.insert(0, self.header)
        return rows

    def append_row(self, values):
        service = self.get_service()
        service.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{self.sheet_name}!A:E",
            valueInputOption="USER_ENTERED",
            body={"values": [values]}
        ).execute()

    def delete_row(self, row_index_sheet):
        """
        row_index_sheet は 1-index（A2 = 1）で渡される。
        Google Sheets API は 0-index なので変換する。
        """
        start = row_index_sheet
        end = row_index_sheet + 1

        creds = service_account.Credentials.from_service_account_file(
            CREDENTIALS_PATH,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        service = build("sheets", "v4", credentials=creds)

        body = {
            "requests": [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": 0,
                            "dimension": "ROWS",
                            "startIndex": start,
                            "endIndex": end
                        }
                    }
                }
            ]
        }
        service.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body=body
        ).execute()

    def find_reservations(self, user=None, day=None, channel=None):
        rows = self.get_values()
        if len(rows) <= 1:
            return []

        matches = []
        for i, row in enumerate(rows[1:], 1):  # 1-index
            if len(row) < 5:
                continue
            if user and row[0] != user:
                continue
            if day and row[2] != day:
                continue
            if channel and row[1] != channel:
                continue
            matches.append({
                "row_index": i,
                "user": row[0],
                "channel": row[1],
                "day": row[2],
                "start": row[3],
                "end": row[4]
            })
        return matches

sheets = SheetOperations()

# --- モーダル（予約） ---
class ReservationModal(ui.Modal, title="☕ 予約情報を入力してください"):
    def __init__(self, channel_name: str):
        super().__init__()
        self.channel_name = channel_name

        self.user_name = ui.TextInput(label="予約者名")
        self.day = ui.TextInput(label="予約日", placeholder="例: 2025/11/01")
        self.start_time = ui.TextInput(label="開始時間", placeholder="例: 13:00")
        self.end_time = ui.TextInput(label="終了時間", placeholder="例: 14:00")

        self.add_item(self.user_name)
        self.add_item(self.day)
        self.add_item(self.start_time)
        self.add_item(self.end_time)

    def is_slot_available(self, day: str, start: str, end: str):
        new_start = datetime.strptime(start, "%H:%M").time()
        new_end = datetime.strptime(end, "%H:%M").time()

        existing = sheets.find_reservations(day=day, channel=self.channel_name)
        for r in existing:
            r_start = datetime.strptime(r["start"], "%H:%M").time()
            r_end = datetime.strptime(r["end"], "%H:%M").time()
            if (new_start < r_end) and (new_end > r_start):
                return False
        return True

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not self.is_slot_available(self.day.value, self.start_time.value, self.end_time.value):
            await interaction.followup.send("❌ この時間帯はすでに予約があります。", ephemeral=True)
            return

        # 登録
        sheets.append_row([
            self.user_name.value,
            self.channel_name,
            self.day.value,
            self.start_time.value,
            self.end_time.value
        ])

        await interaction.followup.send(
            f"✅ 予約を登録しました！"
            f"👤 {self.user_name.value}📅 {self.day.value}"
            f"🏠 {self.channel_name}🕒 {self.start_time.value}〜{self.end_time.value}",
            ephemeral=True
        )


# --- モーダル（キャンセル） ---
class CancelReservationModal(ui.Modal, title="☕ 予約をキャンセルします"):
    def __init__(self, channel_name: str):
        super().__init__()
        self.channel_name = channel_name

        self.user_name = ui.TextInput(label="予約者名")
        self.day = ui.TextInput(label="予約日", placeholder="例: 2025/11/01")
        self.start_time = ui.TextInput(label="開始時間", placeholder="例: 13:00")
        self.end_time = ui.TextInput(label="終了時間", placeholder="例: 14:00")

        self.add_item(self.user_name)
        self.add_item(self.day)
        self.add_item(self.start_time)
        self.add_item(self.end_time)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        matches = sheets.find_reservations(
            user=self.user_name.value,
            day=self.day.value,
            channel=self.channel_name
        )

        matches = [r for r in matches if r["start"] == self.start_time.value and r["end"] == self.end_time.value]

        if not matches:
            await interaction.followup.send("❌ 一致する予約が見つかりませんでした。", ephemeral=True)
            return

        target = matches[0]
        sheets.delete_row(target["row_index"])

        await interaction.followup.send(
            f"✅ 予約をキャンセルしました！"
            f"👤 {target['user']}📅 {target['day']}"
            f"🏠 {target['channel']}🕒 {target['start']}〜{target['end']}",
            ephemeral=True
        )


# --- プルダウンメニュー ---
class MenuSelect(ui.Select):
    def __init__(self, category_channels, is_cancel=False):
        self.is_cancel = is_cancel
        options = []
        for ch in category_channels:
            if isinstance(ch, discord.CategoryChannel):
                continue
            options.append(discord.SelectOption(label=ch.name, value=str(ch.id)))

        super().__init__(
            placeholder="チャンネルを選択してください",
            options=options,
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        channel_id = int(self.values[0])
        channel = interaction.guild.get_channel(channel_id)
        modal = CancelReservationModal(channel.name) if self.is_cancel else ReservationModal(channel.name)
        await interaction.response.send_modal(modal)


class MenuSelectView(ui.View):
    def __init__(self, category_channels, is_cancel=False):
        super().__init__(timeout=60)
        self.add_item(MenuSelect(category_channels, is_cancel))


# --- ボタンメニュー ---
class ReservationMenu(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="📝 予約する", style=discord.ButtonStyle.primary)
    async def reserve_btn(self, interaction: discord.Interaction, button: ui.Button):
        category = interaction.guild.get_channel(CAFE_CATEGORY_ID)
        if not category:
            await interaction.response.send_message("❌ カテゴリーが見つかりません。", ephemeral=True)
            return
        await interaction.response.send_message(
            "メニューを選択してください",
            view=MenuSelectView(category.channels),
            ephemeral=True
        )

    @ui.button(label="❌ キャンセル", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction: discord.Interaction, button: ui.Button):
        category = interaction.guild.get_channel(CAFE_CATEGORY_ID)
        if not category:
            await interaction.response.send_message("❌ カテゴリーが見つかりません。", ephemeral=True)
            return
        await interaction.response.send_message(
            "キャンセルするメニューを選択",
            view=MenuSelectView(category.channels, is_cancel=True),
            ephemeral=True
        )


# --- Slash Commands ---
@bot.tree.command(name="reserve_form", description="予約フォームを表示します")
async def reserve_form(interaction: discord.Interaction):
    category = interaction.guild.get_channel(CAFE_CATEGORY_ID)
    if not category:
        await interaction.response.send_message("❌ カテゴリーが見つかりません。", ephemeral=True)
        return
    await interaction.response.send_message(
        "メニューを選択してください",
        view=MenuSelectView(category.channels),
        ephemeral=True
    )


@bot.tree.command(name="reserve_list", description="予約一覧を表示します")
async def reserve_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    reservations = sheets.find_reservations()
    if not reservations:
        await interaction.followup.send("📭 現在予約はありません。", ephemeral=True)
        return

    embed = discord.Embed(title="☕ 予約一覧（最新10件）", color=discord.Color.green())
    for r in reservations[-10:]:
        embed.add_field(
            name=f"📅 {r['day']} | {r['channel']}",
            value=f"👤 {r['user']}\n🕒 {r['start']}〜{r['end']}",
            inline=False
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="reserve_cancel", description="予約をキャンセルします")
async def reserve_cancel(interaction: discord.Interaction):
    category = interaction.guild.get_channel(CAFE_CATEGORY_ID)
    if not category:
        await interaction.response.send_message("❌ カテゴリーが見つかりません。", ephemeral=True)
        return
    await interaction.response.send_message(
        "キャンセルするメニューを選択してください",
        view=MenuSelectView(category.channels, is_cancel=True),
        ephemeral=True
    )


@bot.tree.command(name="show_menu", description="予約メニューをチャンネルに表示します")
async def show_menu(interaction: discord.Interaction):
    view = ReservationMenu()
    await interaction.response.send_message(
        "操作を選んでください：",
        view=view
    )


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.content.strip() == "カフェ予約":
        view = ReservationMenu()
        await message.channel.send("操作を選んでください！", view=view)
        return
    await bot.process_commands(message)


# --- Bot on_ready ---
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

    # コマンド同期（ギルド優先）
    try:
        if GUILD_OBJ:
            synced = await bot.tree.sync(guild=GUILD_OBJ)
            print(f"🔁 Synced {len(synced)} commands to guild")
        else:
            synced = await bot.tree.sync()
            print(f"🔁 Globally synced {len(synced)} commands")
    except Exception as e:
        print(f"⚠️ Sync failed: {e}")

    # View 永続化
    try:
        bot.add_view(ReservationMenu())
        print("🔁 Persistent ReservationMenu registered")
    except Exception as e:
        print(f"⚠️ Failed to register persistent view: {e}")


# --- Run Bot ---
bot.run(TOKEN)
class ReservationMenu(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="📝 予約する", style=discord.ButtonStyle.primary)
    async def reserve_btn(self, interaction: discord.Interaction, button: ui.Button):
        category = interaction.guild.get_channel(CAFE_CATEGORY_ID)
        if not category:
            await interaction.response.send_message("❌ カテゴリーが見つかりません。", ephemeral=True)
            return
        await interaction.response.send_message("メニューを選択してください", view=MenuSelectView(category.channels), ephemeral=True)

    @ui.button(label="❌ キャンセル", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction: discord.Interaction, button: ui.Button):
        category = interaction.guild.get_channel(CAFE_CATEGORY_ID)
        if not category:
            await interaction.response.send_message("❌ カテゴリーが見つかりません。", ephemeral=True)
            return
        await interaction.response.send_message("キャンセルするメニューを選択", view=MenuSelectView(category.channels, is_cancel=True), ephemeral=True)
