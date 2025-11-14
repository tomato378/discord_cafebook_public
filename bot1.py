import os
import json
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
CAFE_CATEGORY_ID = int(os.getenv("CAFE_CATEGORY_ID", "0"))  # カフェカテゴリのID

# --- Google認証情報切り替え ---
USE_RAILWAY = os.getenv("RAILWAY", "false").lower() == "true"

if USE_RAILWAY:
    # Railwayの場合は環境変数にJSONを入れる
    CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not CREDENTIALS_JSON:
        raise RuntimeError("RAILWAY=true ですが、GOOGLE_CREDENTIALS_JSON が設定されていません。")
    credentials = service_account.Credentials.from_service_account_info(json.loads(CREDENTIALS_JSON))
else:
    # ローカルの場合はファイルパスを使う
    CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH")
    if not CREDENTIALS_PATH or not os.path.exists(CREDENTIALS_PATH):
        raise RuntimeError("RAILWAY=false ですが、CREDENTIALS_PATH が存在しません。")
    credentials = service_account.Credentials.from_service_account_file(CREDENTIALS_PATH)

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

# --- ユーティリティ関数 ---
def format_reservation_message(reservation: dict, prefix: str = "") -> str:
    """予約情報を表示用の文字列にフォーマット"""
    return (
        f"{prefix}\n"
        f"👤 予約者：{reservation['user']}\n"
        f"📅 予約日：{reservation['day']}\n"
        f"🏠 場所：{reservation['channel']}\n"
        f"🕒 時間：{reservation['start']}〜{reservation['end']}"
    ).strip()

def create_reservation_dict(row: list, row_index: int) -> dict:
    """スプレッドシートの行から予約情報の辞書を作成"""
    return {
        "row_index": row_index,
        "user": row[0],
        "channel": row[1],
        "day": row[2],
        "start": row[3],
        "end": row[4]
    }

# --- Google Sheets 操作 ---
class SheetOperations:
    def __init__(self):
        self.service = None
        self.sheet_name = "sheet1"
        self.header = ["ユーザー名", "メニュー名", "日付", "開始", "終了"]

    def get_service(self):
        """Sheets APIサービスを取得（初回のみ初期化）"""
        if not self.service:
            creds = service_account.Credentials.from_service_account_file(
                CREDENTIALS_PATH,
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
            self.service = build("sheets", "v4", credentials=creds).spreadsheets()
        return self.service

    def get_values(self) -> list:
        """シートの全データを取得"""
        service = self.get_service()
        result = service.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{self.sheet_name}!A:E"
        ).execute()
        return result.get("values", [])

    def append_row(self, values: list) -> None:
        """新しい行を追加"""
        service = self.get_service()
        service.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{self.sheet_name}!A:E",
            valueInputOption="USER_ENTERED",
            body={"values": [values]}
        ).execute()

    def delete_row(self, row_index: int) -> None:
        """指定行を削除"""
        service = build("sheets", "v4", credentials=service_account.Credentials.from_service_account_file(
            CREDENTIALS_PATH,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        ))
        body = {
            "requests": [{
                "deleteDimension": {
                    "range": {
                        "sheetId": 0,
                        "dimension": "ROWS",
                        "startIndex": row_index,
                        "endIndex": row_index + 1
                    }
                }
            }]
        }
        service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body=body).execute()

    def find_reservations(self, user: str = None, day: str = None, channel: str = None) -> list:
        """条件に一致する予約を検索"""
        rows = self.get_values()
        if not rows:
            return []

        # ヘッダー行が無い場合は追加
        if rows[0] != self.header:
            self.append_row(self.header)
            return []

        matches = []
        for i, row in enumerate(rows[1:], 1):  # ヘッダーをスキップしてインデックスは1から
            if len(row) < 5:
                continue
            
            if user and row[0] != user:
                continue
            if day and row[2] != day:
                continue
            if channel and row[1] != channel:
                continue
                
            matches.append(create_reservation_dict(row, i))
        
        return matches

sheets = SheetOperations()


# --- モーダル定義（予約用） ---
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
    def is_slot_available(self, day: str, start_time_str: str, end_time_str: str) -> bool:
        """指定した時間枠が予約可能か確認"""
        new_start = datetime.strptime(start_time_str, "%H:%M").time()
        new_end = datetime.strptime(end_time_str, "%H:%M").time()

        # チャンネルと日付で予約を検索
        existing = sheets.find_reservations(day=day, channel=self.channel_name)
        
        for reservation in existing:
            r_start = datetime.strptime(reservation["start"], "%H:%M").time()
            r_end = datetime.strptime(reservation["end"], "%H:%M").time()

            # 重複判定：範囲が少しでも重なる場合は False
            if (new_start < r_end) and (new_end > r_start):
                return False
        return True

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        # 時間範囲重複チェック
        if not self.is_slot_available(self.day.value, self.start_time.value, self.end_time.value):
            await interaction.followup.send(
                f"❌ {self.day.value} {self.start_time.value}〜{self.end_time.value} は既に予約があります。\n"
                f"別の時間を選択してください。",
                ephemeral=True
            )
            return

        # 重複なしなら登録
        try:
            sheets.append_row([
                self.user_name.value,
                self.channel_name,
                self.day.value,
                self.start_time.value,
                self.end_time.value
            ])

            # 登録した予約情報を表示用にフォーマット
            reservation = {
                "user": self.user_name.value,
                "channel": self.channel_name,
                "day": self.day.value,
                "start": self.start_time.value,
                "end": self.end_time.value
            }
            await interaction.followup.send(
                format_reservation_message(reservation, prefix="✅ 予約を登録しました！"),
                ephemeral=False
            )
        except Exception as e:
            await interaction.followup.send(
                f"❌ エラーが発生しました: {e}", ephemeral=True
            )

# --- モーダル定義（キャンセル用） ---
class CancelReservationModal(ui.Modal, title="☕ キャンセルしたい予約情報を入力してください"):
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

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        # 条件に一致する予約を探す
        matches = sheets.find_reservations(
            user=self.user_name.value,
            day=self.day.value,
            channel=self.channel_name
        )

        # 開始時間と終了時間で絞り込み
        matches = [
            r for r in matches
            if r["start"] == self.start_time.value and r["end"] == self.end_time.value
        ]

        if not matches:
            await interaction.followup.send(
                "❌ 入力された予約情報は見つかりませんでした。",
                ephemeral=True
            )
            return

        # 最初に見つかった予約をキャンセル
        reservation = matches[0]
        try:
            sheets.delete_row(reservation["row_index"])
            await interaction.followup.send(
                format_reservation_message(reservation, prefix="✅ 予約をキャンセルしました！"),
                ephemeral=False
            )
        except Exception as e:
            await interaction.followup.send(
                f"❌ キャンセル中にエラーが発生しました: {e}",
                ephemeral=True
            )

# --- プルダウンメニュー定義 ---
class MenuSelect(ui.Select):
    def __init__(self, category_channels, is_cancel=False):
        self.is_cancel = is_cancel
        action = "キャンセル" if is_cancel else "予約"
        options = [
            discord.SelectOption(
                label=ch.name,
                description=f"{'ボイスチャンネル' if isinstance(ch, discord.VoiceChannel) else 'テキストチャンネル'} を{action}"
            )
            for ch in category_channels
            if isinstance(ch, (discord.TextChannel, discord.VoiceChannel))
        ]
        super().__init__(
            placeholder=f"チャンネルを選択してください ☕",
            options=options,
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        channel_name = self.values[0]
        modal = CancelReservationModal(channel_name) if self.is_cancel else ReservationModal(channel_name)
        await interaction.response.send_modal(modal)

# --- View定義 ---
class MenuSelectView(ui.View):
    def __init__(self, category_channels, is_cancel=False):
        super().__init__(timeout=60)
        self.add_item(MenuSelect(category_channels, is_cancel))

# --- 予約フォームコマンド ---
@bot.tree.command(name="reserve_form", description="ポップアップで予約を登録します")
async def reserve_form(interaction: discord.Interaction):
    category = interaction.guild.get_channel(CAFE_CATEGORY_ID)

    if not category or not isinstance(category, discord.CategoryChannel):
        await interaction.response.send_message(
            f"❌ カテゴリーが見つかりません。(ID: {CAFE_CATEGORY_ID})\n"
            f"管理者に確認してください。",
            ephemeral=True
        )
        return

    view = MenuSelectView(category.channels)
    await interaction.response.send_message("☕ メニューを選んでください：", view=view, ephemeral=False)

# --- 予約一覧コマンド ---
@bot.tree.command(name="reserve_list", description="予約一覧を表示します")
async def reserve_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    reservations = sheets.find_reservations()  # 全予約を取得

    if not reservations:
        await interaction.followup.send("📭 現在、予約はありません。", ephemeral=True)
        return

    embed = discord.Embed(title="☕ 予約一覧（最新10件）", color=discord.Color.green())

    # 最新の10件を表示
    for reservation in reservations[-10:]:
        embed.add_field(
            name=f"📅 {reservation['day']} | {reservation['channel']}",
            value=f"👤 {reservation['user']}\n🕒 {reservation['start']}〜{reservation['end']}",
            inline=False
        )

    await interaction.followup.send(embed=embed, ephemeral=True)

# --- 予約キャンセルコマンド ---
@bot.tree.command(name="reserve_cancel", description="予約をキャンセルします")
async def reserve_cancel(interaction: discord.Interaction):
    category = interaction.guild.get_channel(CAFE_CATEGORY_ID)
    
    if not category or not isinstance(category, discord.CategoryChannel):
        await interaction.response.send_message(
            f"❌ カテゴリーが見つかりません。(ID: {CAFE_CATEGORY_ID})\n"
            f"管理者に確認してください。",
            ephemeral=True
        )
        return

    # チャンネル選択ビューを表示
    view = MenuSelectView(category.channels, is_cancel=True)
    await interaction.response.send_message("☕ メニューを選んでください：", view=view, ephemeral=False)

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
