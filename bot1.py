import os
import discord
from discord.ext import commands
from discord import app_commands, ui
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime

# --- 環境変数読み込み ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
SPREADSHEET_ID = os.getenv("GOOGLE_SHEET_ID")
CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH")
# --- GUILD ID の読み取り（テスト時は .env に GUILD_ID を入れてください） ---
GUILD_ID_ENV = os.getenv("GUILD_ID")
if GUILD_ID_ENV:
    try:
        GUILD_ID = int(GUILD_ID_ENV)
        GUILD_OBJ = discord.Object(id=GUILD_ID)
    except Exception:
        print(f"⚠️ Invalid GUILD_ID environment variable: {GUILD_ID_ENV!r}")
        GUILD_ID = None
        GUILD_OBJ = None
else:
    GUILD_ID = None
    GUILD_OBJ = None


# 条件付きで @app_commands.guilds デコレータを適用するユーティリティ
# NOTE: ギルドスコープは on_ready での guild sync により即時反映できます。
# そのため個別コマンドにデコレータを付ける必要はありません。
# 以前は maybe_guild_decorator を使っていましたが、デコレータの適用順による
# 想定外の動作を避けるため廃止しました。

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

SHEET_NAME = "sheet1"

sheet = get_sheets_service()

# 読み込み
result = sheet.values().get(
    spreadsheetId=SPREADSHEET_ID,
    range=f"{SHEET_NAME}!A:E"
).execute()

sheetvalues = result.get("values", [])

# データが空の場合のみヘッダーを書き込む
if not sheetvalues:
    header = [["ユーザー名", "メニュー名", "日付", "開始", "終了"]]
    sheet.values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A:E",
        valueInputOption="USER_ENTERED",
        body={"values": header}
    ).execute()


# --- モーダル定義（重複チェック追加） ---
class ReservationModal(ui.Modal, title="☕ 予約情報を入力してください"):
    def __init__(self, channel_name: str):
        super().__init__()
        self.channel_name = channel_name

        self.user_name = ui.TextInput(label="予約者名", placeholder="キャンセルの際に必要です")
        self.day = ui.TextInput(label="予約日", default="2025/11/01", placeholder="例: 2025/11/01")
        self.start_time = ui.TextInput(label="開始時間", placeholder="例: 13:00(半角)")
        self.end_time = ui.TextInput(label="終了時間", placeholder="例: 14:00(半角)")

        self.add_item(self.user_name)
        self.add_item(self.day)
        self.add_item(self.start_time)
        self.add_item(self.end_time)

    # --- 重複チェック（開始〜終了時間範囲） ---
    def is_slot_available(self, day, start_time_str, end_time_str):
        sheet = get_sheets_service()
        result = sheet.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{SHEET_NAME}!A:E"
        ).execute()
        rows = result.get("values", [])

        new_start = datetime.strptime(start_time_str, "%H:%M").time()
        new_end = datetime.strptime(end_time_str, "%H:%M").time()

        for row in rows:
            if len(row) >= 5:
                _, channel, r_day, r_start_str, r_end_str = row
                if channel != self.channel_name or r_day != day:
                    continue

                r_start = datetime.strptime(r_start_str, "%H:%M").time()
                r_end = datetime.strptime(r_end_str, "%H:%M").time()

                # 重複判定：範囲が少しでも重なる場合は False
                if (new_start < r_end) and (new_end > r_start):
                    return False
        return True

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        # 時間範囲重複チェック
        if not self.is_slot_available(self.day.value, self.start_time.value, self.end_time.value):
            await interaction.followup.send(
                f"❌ {self.day.value} {self.start_time.value}〜{self.end_time.value} は既に予約があります。\n"
                f"別の時間を選択してください。",
                ephemeral=True
            )
            return

        # 重複なしなら登録
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
                range=f"{SHEET_NAME}!A:E",
                valueInputOption="USER_ENTERED",
                body={"values": values}
            ).execute()
            await interaction.response.send_message(
                f"✅ {self.user_name.value} さんの予約を登録しました！\n"
                f"🧾 {self.channel_name} チャンネル\n"
                f"📅 {self.day.value}\n"
                f"🕒 {self.start_time.value}〜{self.end_time.value}",
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

# --- 予約フォームコマンド ---
@bot.tree.command(name="reserve_form", description="ポップアップで予約を登録します")
async def reserve_form(interaction: discord.Interaction):
    category = discord.utils.get(interaction.guild.categories, name="カフェ")

    if not category:
        await interaction.response.send_message("❌ 『カフェ』カテゴリーが見つかりません。", ephemeral=True)
        return

    view = MenuSelectView(category.channels)
    await interaction.response.send_message("☕ メニューを選んでください：", view=view, ephemeral=True)

# --- 予約一覧コマンド ---
@bot.tree.command(name="reserve_list", description="予約一覧を表示します")
async def reserve_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    sheet = get_sheets_service()
    result = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A:E"
    ).execute()


    values = result.get("values", [])[1:]  # ヘッダー行を除外

    if not values:
        await interaction.followup.send("📭 現在、予約はありません。", ephemeral=True)
        return

    embed = discord.Embed(title="☕ 予約一覧（最新10件）", color=discord.Color.green())

    for row in values[-10:]:
        if len(row) >= 5:
            user, channel, day, start, end = row
            embed.add_field(
                name=f"📅 {day} | {channel}",
                value=f"👤 {user}\n🕒 {start}〜{end}",
                inline=False
            )

    await interaction.followup.send(embed=embed, ephemeral=True)

# --- Bot起動 ---
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    try:
        # デバッグ出力：bot.tree にロードされているコマンドの一覧を表示
        try:
            global_cmds = [c.name for c in bot.tree.get_commands()]
        except Exception:
            global_cmds = []
        try:
            walk_cmds = [c.name for c in bot.tree.walk_commands()]
        except Exception:
            walk_cmds = []
        print(f"🔎 debug: tree.get_commands() => {global_cmds}")
        print(f"🔎 debug: tree.walk_commands() => {walk_cmds}")

        # 追加デバッグ：application id / application info / bot user id
        try:
            print(f"🔎 debug: bot.user.id = {bot.user.id}")
        except Exception:
            print("🔎 debug: bot.user.id unavailable")
        try:
            print(f"🔎 debug: bot.application_id = {bot.application_id}")
        except Exception:
            print("🔎 debug: bot.application_id unavailable")
        try:
            app_info = await bot.application_info()
            print(f"🔎 debug: application_info: id={getattr(app_info,'id',None)} name={getattr(app_info,'name',None)}")
        except Exception as e:
            print(f"🔎 debug: application_info fetch failed: {e}")

        # 各コマンドの詳細（repr と属性）を表示
        try:
            for c in bot.tree.walk_commands():
                try:
                    attrs = {
                        'name': getattr(c, 'name', None),
                        'description': getattr(c, 'description', None),
                        'guilds': getattr(c, 'guilds', None),
                        'qualified_name': getattr(c, 'qualified_name', None)
                    }
                except Exception:
                    attrs = {'name': getattr(c, 'name', None)}
                print(f"🔎 debug: command object -> {c!r} attrs={attrs}")
        except Exception as e:
            print(f"🔎 debug: walk_commands failed: {e}")

        # --- 開発用：ギルド同期で即時コマンド反映 ---
        if GUILD_OBJ:
            # Explicitly ensure each command is added to the guild mapping before syncing.
            added = []
            for c in bot.tree.walk_commands():
                try:
                    # add_command(command, guild=...) will copy the command into the guild-specific mapping
                    bot.tree.add_command(c, guild=GUILD_OBJ)
                    added.append(getattr(c, 'name', repr(c)))
                except Exception as e:
                    print(f"⚠️ failed to add command {getattr(c,'name',repr(c))} to guild mapping: {e}")

            print(f"🔁 debug: attempted to add commands to guild mapping => {added}")
            synced = await bot.tree.sync(guild=GUILD_OBJ)
            print(f"🔁 Slash commands synced to guild ({len(synced)} commands)")
            # 起動後に現在登録されているコマンド一覧を確認
            try:
                guild_cmds = bot.tree.get_commands(guild=GUILD_OBJ)
            except Exception:
                guild_cmds = []
            print(f"🔁 guild commands after sync: {guild_cmds}")
        else:
            print("⚠️ GUILD_ID が設定されていません。ギルド同期はスキップされます。開発時は .env に GUILD_ID を設定してください。")

        # --- 本番用グローバル同期（必要なら以下をアンコメント） ---
        # グローバル登録は反映に最大1時間程度かかるため、開発中はギルド同期を推奨します。
        # try:
        #     synced_global = await bot.tree.sync()
        #     print(f"🔁 Slash commands synced globally ({len(synced_global)} commands)")
        # except Exception as e:
        #     print(f"⚠️ Global sync failed: {e}")

    except Exception as e:
        print(f"⚠️ Sync failed: {e}")



bot.run(TOKEN)
