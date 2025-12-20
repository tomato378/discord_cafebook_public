import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from aiohttp import web

# --- 環境変数 ---
load_dotenv()


def _read_int(name: str) -> Optional[int]:
    value = os.getenv(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


TEST_SERVER = os.getenv("TEST_SERVER", "false").lower() == "true"

TOKEN = os.getenv("DISCORD_TOKEN")
SPREADSHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "sheet1")

if TEST_SERVER:
    CAFE_CATEGORY_ID = _read_int("CAFE_CATEGORY_ID_TEST") or 0
    CAFE_CATEGORY_NAME = os.getenv("CAFE_CATEGORY_NAME_TEST", "").strip()
    GUILD_ID = _read_int("GUILD_ID_TEST")
    RESERVATION_ANNOUNCE_CHANNEL_ID = _read_int("RESERVATION_ANNOUNCE_CHANNEL_ID_TEST") or 0
    REMINDER_CHANNEL_ID = _read_int("REMINDER_CHANNEL_ID_TEST") or 0
    REMINDER_MINUTES_BEFORE = (
        _read_int("REMINDER_MINUTES_BEFORE_TEST")
        or _read_int("REMINDER_MINUTES_BEFORE")
        or 15
    )
else:
    CAFE_CATEGORY_ID = _read_int("CAFE_CATEGORY_ID") or 0
    CAFE_CATEGORY_NAME = os.getenv("CAFE_CATEGORY_NAME", "").strip()
    GUILD_ID = _read_int("GUILD_ID")
    RESERVATION_ANNOUNCE_CHANNEL_ID = _read_int("RESERVATION_ANNOUNCE_CHANNEL_ID") or 0
    REMINDER_CHANNEL_ID = _read_int("REMINDER_CHANNEL_ID") or 0
    REMINDER_MINUTES_BEFORE = _read_int("REMINDER_MINUTES_BEFORE") or 15
CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")

GUILD_OBJ = discord.Object(id=GUILD_ID) if GUILD_ID else None
JST = timezone(timedelta(hours=9))


def _maybe_guild_scope(func):
    if TEST_SERVER and GUILD_OBJ:
        return app_commands.guilds(GUILD_OBJ)(func)
    return func


def resolve_cafe_category(guild: Optional[discord.Guild]) -> Optional[discord.CategoryChannel]:
    if not guild:
        return None
    if CAFE_CATEGORY_ID:
        ch = guild.get_channel(CAFE_CATEGORY_ID)
        if isinstance(ch, discord.CategoryChannel):
            return ch
    if CAFE_CATEGORY_NAME:
        # 完全一致を優先
        for cat in guild.categories:
            if cat.name == CAFE_CATEGORY_NAME:
                return cat
        # 部分一致も試す
        lowered = CAFE_CATEGORY_NAME.lower()
        for cat in guild.categories:
            if lowered in cat.name.lower():
                return cat
    return None


def _category_hint(guild: Optional[discord.Guild]) -> str:
    names = []
    if guild:
        names = [cat.name for cat in guild.categories]
    return (
        "カテゴリが見つかりません。\n"
        f"設定ID: {CAFE_CATEGORY_ID or '未設定'} / 設定NAME: {CAFE_CATEGORY_NAME or '未設定'}\n"
        f"ギルド内カテゴリ一覧: {', '.join(names) if names else '取得できませんでした'}"
    )


async def _health_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _start_health_server():
    global _health_app_started
    if _health_app_started:
        return
    _health_app_started = True
    app = web.Application()
    app.add_routes([web.get("/", _health_handler), web.get("/health", _health_handler)])
    port = int(os.getenv("PORT", "10000"))
    if port <= 0:
        port = 10000
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Health server running on 0.0.0.0:{port}")
# --- Bot 設定 ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)
_health_app_started = False


# --- ユーティリティ ---
def parse_time(text: str) -> datetime.time:
    return datetime.strptime(text, "%H:%M").time()


def overlaps(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    sa = parse_time(start_a)
    ea = parse_time(end_a)
    sb = parse_time(start_b)
    eb = parse_time(end_b)
    return max(sa, sb) < min(ea, eb)


def ensure_token() -> None:
    if not TOKEN or not SPREADSHEET_ID:
        raise RuntimeError("DISCORD_TOKEN と GOOGLE_SHEET_ID を設定してください。")
    if CAFE_CATEGORY_ID <= 0 and not CAFE_CATEGORY_NAME:
        raise RuntimeError("CAFE_CATEGORY_ID (または CAFE_CATEGORY_ID_TEST) か CAFE_CATEGORY_NAME(_TEST) を設定してください。")
    # 起動時に認証情報も確認する
    load_credentials()


def is_past_reservation(day: str, end: str) -> bool:
    try:
        end_dt = datetime.strptime(f"{day} {end}", "%Y/%m/%d %H:%M").replace(tzinfo=JST)
    except ValueError:
        return False
    return end_dt < datetime.now(JST)


def load_credentials():
    # 1. GOOGLE_CREDENTIALS_JSON（環境変数）のチェック
    json_blob = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if json_blob:
        info = json.loads(json_blob)
        return service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )

    # 2. GOOGLE_CREDENTIALS_PATH（ファイルパス）のチェック
    explicit_path = os.getenv("GOOGLE_CREDENTIALS_PATH")
    if explicit_path and os.path.exists(explicit_path):
        return service_account.Credentials.from_service_account_file(
            explicit_path,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )

    # 3. Secret Files（シークレットファイル）のチェック
    secret_file_path = "/etc/secrets/credentials.json"
    if os.path.exists(secret_file_path):
        return service_account.Credentials.from_service_account_file(
            secret_file_path,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )

    # 4. ローカルファイルのチェック
    local_path = "credentials.json"
    if os.path.exists(local_path):
        return service_account.Credentials.from_service_account_file(
            local_path,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )

    raise RuntimeError("Google 認証情報が見つかりません。")

# --- Google Sheet 操作 ---
class SheetOperations:
    def __init__(self) -> None:
        self.service = None
        self.sheet_name = SHEET_NAME
        self.header = ["予約者", "チャンネル", "日付", "開始", "終了", "予約者ID", "参加者JSON", "作成日時", "reminded"]
        self.sheet_id: Optional[int] = None

    def _get_api(self):
        if not self.service:
            creds = load_credentials()
            self.service = build("sheets", "v4", credentials=creds).spreadsheets()
        return self.service

    def _ensure_sheet_id(self) -> int:
        if self.sheet_id is not None:
            return self.sheet_id
        api = self._get_api()
        info = api.get(spreadsheetId=SPREADSHEET_ID).execute()
        for sheet in info.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == self.sheet_name:
                self.sheet_id = props.get("sheetId", 0)
                return self.sheet_id
        # fallback to first sheet
        self.sheet_id = info.get("sheets", [{}])[0].get("properties", {}).get("sheetId", 0)
        return self.sheet_id

    def ensure_header_row(self) -> None:
        api = self._get_api()
        result = api.values().get(
            spreadsheetId=SPREADSHEET_ID, range=f"{self.sheet_name}!A1:I1"
        ).execute()
        values = result.get("values", [])
        if not values:
            api.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{self.sheet_name}!A1:I1",
                valueInputOption="RAW",
                body={"values": [self.header]},
            ).execute()
            return
        if values[0] != self.header:
            sheet_id = self._ensure_sheet_id()
            api.batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body={
                    "requests": [
                        {
                            "insertDimension": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "dimension": "ROWS",
                                    "startIndex": 0,
                                    "endIndex": 1,
                                },
                                "inheritFromBefore": False,
                            }
                        }
                    ]
                },
            ).execute()
            api.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{self.sheet_name}!A1:I1",
                valueInputOption="RAW",
                body={"values": [self.header]},
            ).execute()

    def fetch_rows(self) -> List[Tuple[int, List[str]]]:
        self.ensure_header_row()
        api = self._get_api()
        result = api.values().get(
            spreadsheetId=SPREADSHEET_ID, range=f"{self.sheet_name}!A:I"
        ).execute()
        rows = result.get("values", [])
        data: List[Tuple[int, List[str]]] = []
        for idx, row in enumerate(rows, start=1):
            if idx == 1:
                continue
            padded = row + [""] * max(0, 9 - len(row))
            data.append((idx, padded[:9]))
        return data

    def append_row(
        self,
        user_mention: str,
        channel_name: str,
        day: str,
        start: str,
        end: str,
        user_id: int,
    ) -> int:
        self.ensure_header_row()
        api = self._get_api()
        values = [
            user_mention,
            channel_name,
            day,
            start,
            end,
            str(user_id),
            "[]",
            datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S"),
            "FALSE",
        ]
        response = (
            api.values()
            .append(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{self.sheet_name}!A:H",
                valueInputOption="USER_ENTERED",
                body={"values": [values]},
            )
            .execute()
        )
        updated = response.get("updates", {})
        updated_range = updated.get("updatedRange", "")
        row_number = 0
        try:
            row_part = updated_range.split("!")[1]
            row_number = int(row_part.split(":")[0][1:])
        except Exception:
            row_number = 0
        return row_number

    def update_participants(self, row_index: int, participants: Sequence[Dict[str, str]]) -> None:
        api = self._get_api()
        payload = json.dumps(list(participants), ensure_ascii=False)
        api.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{self.sheet_name}!G{row_index}",
            valueInputOption="RAW",
            body={"values": [[payload]]},
        ).execute()

    def mark_reminded(self, row_index: int) -> None:
        api = self._get_api()
        api.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{self.sheet_name}!I{row_index}",
            valueInputOption="RAW",
            body={"values": [["TRUE"]]},
        ).execute()

    def delete_row(self, row_index: int) -> None:
        sheet_id = self._ensure_sheet_id()
        api = self._get_api()
        api.batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={
                "requests": [
                    {
                        "deleteDimension": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "ROWS",
                                "startIndex": row_index - 1,
                                "endIndex": row_index,
                            }
                        }
                    }
                ]
            },
        ).execute()

    def is_slot_available(self, channel_name: str, day: str, start: str, end: str) -> bool:
        for _, row in self.fetch_rows():
            row_channel, row_day, row_start, row_end = row[1], row[2], row[3], row[4]
            if not row_channel or not row_day:
                continue
            if row_channel != channel_name or row_day != day:
                continue
            if overlaps(start, end, row_start, row_end):
                return False
        return True

    def find_by_user(self, user_id: int) -> List[Dict[str, str]]:
        results: List[Dict[str, str]] = []
        for idx, row in self.fetch_rows():
            if row[5] != str(user_id):
                continue
            results.append(
                {
                    "row_index": idx,
                    "user": row[0],
                    "channel": row[1],
                    "day": row[2],
                    "start": row[3],
                    "end": row[4],
                    "participants": row[6],
                    "created_at": row[7],
                }
            )
        return results


sheets = SheetOperations()


# --- UI コンポーネント ---
class TimeInputModal(ui.Modal, title="☕ 予約時間を入力"):
    def __init__(self, user: discord.User):
        super().__init__(timeout=300)
        self.request_user = user
        self.day = ui.TextInput(label="日付 (YYYY/MM/DD)", default=datetime.now(JST).strftime("%Y/%m/%d"))
        self.start_time = ui.TextInput(label="開始 (HH:MM)", default="13:00")
        self.end_time = ui.TextInput(label="終了 (HH:MM)", default="14:00")
        self.add_item(self.day)
        self.add_item(self.start_time)
        self.add_item(self.end_time)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            datetime.strptime(self.day.value, "%Y/%m/%d")
            start_t = parse_time(self.start_time.value)
            end_t = parse_time(self.end_time.value)
        except ValueError:
            await interaction.response.send_message("日付または時間の形式が正しくありません。", ephemeral=True)
            return

        if start_t >= end_t:
            await interaction.response.send_message("開始時間は終了時間より前にしてください。", ephemeral=True)
            return

        category = resolve_cafe_category(interaction.guild)
        if not category or not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(_category_hint(interaction.guild), ephemeral=True)
            return

        candidates = [
            ch for ch in category.channels if isinstance(ch, discord.VoiceChannel)
        ]
        available = [
            ch for ch in candidates
            if sheets.is_slot_available(ch.name, self.day.value, self.start_time.value, self.end_time.value)
        ]
        if not available:
            await interaction.response.send_message("指定時間に空いている席がありません。", ephemeral=True)
            return

        view = ChannelSelectView(
            user=interaction.user,
            channels=available,
            day=self.day.value,
            start=self.start_time.value,
            end=self.end_time.value,
        )
        await interaction.response.send_message(
            f"{self.day.value} {self.start_time.value}〜{self.end_time.value} で予約する席を選んでください。",
            view=view,
            ephemeral=True,
        )


class ChannelSelect(ui.Select):
    def __init__(self, parent: "ChannelSelectView"):
        options = [
            discord.SelectOption(label=ch.name, value=str(ch.id)) for ch in parent.channels
        ]
        super().__init__(placeholder="席を選択", min_values=1, max_values=1, options=options[:25])
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent_view.user.id:
            await interaction.response.send_message("予約の作成者のみ操作できます。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        channel_id = int(self.values[0])
        channel = discord.utils.get(self.parent_view.channels, id=channel_id)
        if not channel:
            await interaction.followup.send("チャンネルが見つかりません。", ephemeral=True)
            return

        row_index = sheets.append_row(
            user_mention=interaction.user.mention,
            channel_name=channel.name,
            day=self.parent_view.day,
            start=self.parent_view.start,
            end=self.parent_view.end,
            user_id=interaction.user.id,
        )

        participant_view = ParticipantSelectView(
            row_index=row_index,
            owner=interaction.user,
            channel_name=channel.name,
            day=self.parent_view.day,
            start=self.parent_view.start,
            end=self.parent_view.end,
            announce_channel=interaction.guild.get_channel(RESERVATION_ANNOUNCE_CHANNEL_ID) if interaction.guild else None,
            user_mention=interaction.user.mention,
        )
        await interaction.followup.send(
            content=(
                "予約を登録しました。\n"
                f"席: {channel.name}\n"
                f"日付: {self.parent_view.day}\n"
                f"時間: {self.parent_view.start}〜{self.parent_view.end}\n"
                "参加者を追加しますか？（任意・スキップ可）"
            ),
            view=participant_view,
            ephemeral=True,
        )
        if RESERVATION_ANNOUNCE_CHANNEL_ID and participant_view.announce_channel is None:
            await interaction.followup.send("指定のアナウンスチャンネルが見つかりませんでした。", ephemeral=True)


class ChannelSelectView(ui.View):
    def __init__(self, user: discord.User, channels: Sequence[discord.VoiceChannel], day: str, start: str, end: str):
        super().__init__(timeout=180)
        self.user = user
        self.channels = list(channels)
        self.day = day
        self.start = start
        self.end = end
        self.add_item(ChannelSelect(self))


class ParticipantSelect(ui.UserSelect):
    def __init__(self, parent: "ParticipantSelectView"):
        super().__init__(
            placeholder="参加者を選択（任意）",
            min_values=0,
            max_values=10,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent_view.owner.id:
            await interaction.response.send_message("予約者のみ参加者を登録できます。", ephemeral=True)
            return
        participants = [
            {"id": str(member.id), "name": member.mention} for member in self.values
        ]
        sheets.update_participants(self.parent_view.row_index, participants)
        names = ", ".join(member.mention for member in self.values) if self.values else "なし"
        await self.parent_view._send_announce(participants_text=names)
        await interaction.response.edit_message(view=None)


class ParticipantSelectView(ui.View):
    def __init__(self, row_index: int, owner: discord.User, channel_name: str, day: str, start: str, end: str, announce_channel: Optional[discord.TextChannel], user_mention: str):
        super().__init__(timeout=180)
        self.row_index = row_index
        self.owner = owner
        self.channel_name = channel_name
        self.day = day
        self.start = start
        self.end = end
        self.announce_channel = announce_channel
        self.user_mention = user_mention
        self.add_item(ParticipantSelect(self))

    @ui.button(label="スキップ", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, _: ui.Button):
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("予約者のみ操作できます。", ephemeral=True)
            return
        await self._send_announce(participants_text="なし")
        await interaction.response.edit_message(content="予約が完了しました。", view=None)

    async def _send_announce(self, participants_text: str):
        if not self.announce_channel:
            return
        embed = discord.Embed(
            title="☕ 予約が作成されました",
            description=f"{self.user_mention} が {self.channel_name} を予約しました。",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="日付", value=self.day, inline=True)
        embed.add_field(name="時間", value=f"{self.start}〜{self.end}", inline=True)
        embed.add_field(name="参加者", value=participants_text or "なし", inline=False)
        try:
            await self.announce_channel.send(embed=embed)
        except discord.HTTPException:
            pass


class CancelButtonView(ui.View):
    def __init__(self, row_index: int):
        super().__init__(timeout=120)
        self.row_index = row_index

    @ui.button(label="キャンセルする", style=discord.ButtonStyle.danger)
    async def do_cancel(self, interaction: discord.Interaction, _: ui.Button):
        sheets.delete_row(self.row_index)
        await interaction.response.edit_message(content="予約をキャンセルしました。", view=None)


class ReservationMenu(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="📝 予約する", style=discord.ButtonStyle.primary, custom_id="cafebook2:reserve")
    async def reserve_btn(self, interaction: discord.Interaction, _: ui.Button):
        if interaction.response.is_done():
            return
        try:
            await interaction.response.send_modal(TimeInputModal(interaction.user))
        except discord.NotFound:
            return
        except discord.HTTPException as e:
            # Interaction already acknowledged等は握りつぶす
            if e.code != 40060:
                raise

    @ui.button(label="❌ キャンセル", style=discord.ButtonStyle.danger, custom_id="cafebook2:cancel")
    async def cancel_btn(self, interaction: discord.Interaction, _: ui.Button):
        if interaction.response.is_done():
            return
        await send_cancellation_embeds(interaction)


# --- コマンド & イベント ---
async def send_cancellation_embeds(interaction: discord.Interaction):
    matches = [
        res for res in sheets.find_by_user(interaction.user.id)
        if not is_past_reservation(res["day"], res["end"])
    ]
    if not matches:
        try:
            if interaction.response.is_done():
                await interaction.followup.send("あなたの予約は見つかりませんでした。", ephemeral=True)
            else:
                await interaction.response.send_message("あなたの予約は見つかりませんでした。", ephemeral=True)
        except discord.HTTPException as e:
            if e.code != 40060:
                raise
        return

    await interaction.response.defer(ephemeral=True)

    for res in matches:
        embed = discord.Embed(title="予約内容", color=discord.Color.orange())
        embed.add_field(name="チャンネル", value=res["channel"], inline=True)
        embed.add_field(name="日付", value=res["day"], inline=True)
        embed.add_field(name="時間", value=f"{res['start']}〜{res['end']}", inline=True)
        participants = res.get("participants") or "[]"
        try:
            parsed_mentions = parse_participant_mentions(participants)
            mention_text = ", ".join(parsed_mentions) if parsed_mentions else "なし"
        except json.JSONDecodeError:
            mention_text = "なし"
        embed.add_field(name="参加者", value=mention_text, inline=False)
        view = CancelButtonView(res["row_index"])
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@_maybe_guild_scope
@bot.tree.command(name="reserve_form", description="予約フォームを表示")
async def reserve_form(interaction: discord.Interaction):
    await interaction.response.send_modal(TimeInputModal(interaction.user))


@_maybe_guild_scope
@bot.tree.command(name="reserve_cancel", description="自分の予約をキャンセル")
async def reserve_cancel(interaction: discord.Interaction):
    await send_cancellation_embeds(interaction)


@_maybe_guild_scope
@bot.tree.command(name="show_menu", description="予約メニューを表示")
async def show_menu(interaction: discord.Interaction):
    view = ReservationMenu()
    await interaction.response.send_message("操作を選んでください。", view=view)


@_maybe_guild_scope
@bot.tree.command(name="cafebook_panel", description="(互換) 旧コマンド: 予約メニューを表示")
async def cafebook_panel(interaction: discord.Interaction):
    view = ReservationMenu()
    try:
        await interaction.response.send_message("操作を選んでください。", view=view)
    except discord.NotFound:
        # 古いメッセージや無効トークンで呼ばれた場合は握りつぶす
        return


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.content.strip() == "カフェ予約":
        view = ReservationMenu()
        await message.channel.send("操作を選んでください。", view=view)
        return
    await bot.process_commands(message)


# --- リマインド ---
def parse_participant_mentions(raw: str) -> List[str]:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    mentions: List[str] = []
    for item in data:
        if isinstance(item, dict):
            member_id = item.get("id")
            name = item.get("name")
            if member_id:
                mentions.append(f"<@{member_id}>")
            elif name:
                mentions.append(str(name))
        else:
            try:
                member_id = int(item)
                mentions.append(f"<@{member_id}>")
            except (TypeError, ValueError):
                continue
    return mentions


@tasks.loop(minutes=1)
async def reminder_loop():
    if REMINDER_MINUTES_BEFORE <= 0 or REMINDER_CHANNEL_ID <= 0:
        return
    channel = bot.get_channel(REMINDER_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(REMINDER_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    now = datetime.now(JST)
    today_key = now.strftime("%Y/%m/%d")
    for row_index, row in sheets.fetch_rows():
        reminded = (row[8] or "").strip().lower() == "true"
        if reminded:
            continue
        day = row[2]
        start = row[3]
        if not day or not start:
            continue
        if day != today_key:
            continue
        try:
            start_dt = datetime.strptime(f"{day} {start}", "%Y/%m/%d %H:%M").replace(tzinfo=JST)
        except ValueError:
            continue
        delta = start_dt - now
        if timedelta(0) <= delta <= timedelta(minutes=REMINDER_MINUTES_BEFORE):
            mention_ids = []
            seen_ids = set()
            try:
                owner_id = int(row[5])
                if owner_id not in seen_ids:
                    seen_ids.add(owner_id)
                    mention_ids.append(owner_id)
            except (TypeError, ValueError):
                pass
            try:
                raw_participants = row[6]
                data = json.loads(raw_participants) if raw_participants else []
            except (json.JSONDecodeError, TypeError):
                data = []
            if isinstance(data, list):
                for item in data:
                    candidate_id = item.get("id") if isinstance(item, dict) else item
                    try:
                        pid = int(candidate_id)
                    except (TypeError, ValueError):
                        continue
                    if pid in seen_ids:
                        continue
                    seen_ids.add(pid)
                    mention_ids.append(pid)
            mention_text = " ".join(f"<@{uid}>" for uid in mention_ids).strip()
            try:
                await channel.send(
                    f"{mention_text}\n開始 {REMINDER_MINUTES_BEFORE} 分前です！ {day} {row[3]}〜{row[4]} / {row[1]}",
                    allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=uid) for uid in mention_ids]),
                )
            except discord.HTTPException:
                continue
            sheets.mark_reminded(row_index)


@reminder_loop.before_loop
async def before_reminder_loop():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    ensure_token()
    try:
        cmds = [cmd.name for cmd in bot.tree.walk_commands()]
        print(f"📋 Loaded commands before sync: {cmds}")
        if GUILD_OBJ and TEST_SERVER:
            guild = bot.get_guild(GUILD_ID)
            if guild is None:
                print(f"⚠️ Bot is not in guild {GUILD_ID}. Invite it with the applications.commands scope.")
            synced = await bot.tree.sync(guild=GUILD_OBJ)
            print(f"🔁 Synced {len(synced)} commands to guild {GUILD_ID}")
            fetched = await bot.tree.fetch_commands(guild=GUILD_OBJ)
            print(f"📡 Remote guild commands: {[c.name for c in fetched]}")
            if len(fetched) == 0:
                print("⚠️ Guild sync returned 0. Check GUILD_ID(_TEST) and that the bot was invited with applications.commands. No global registration performed.")
        else:
            synced = await bot.tree.sync()
            print(f"🔁 Globally synced {len(synced)} commands")
            fetched = await bot.tree.fetch_commands()
            print(f"📡 Remote global commands: {[c.name for c in fetched]}")
        bot.add_view(ReservationMenu())
        if not reminder_loop.is_running():
            reminder_loop.start()
        print(f"☕ bot ready as {bot.user} (TEST_SERVER={TEST_SERVER}, GUILD_ID={GUILD_ID})")
        await _start_health_server()
    except Exception as exc:
        print(f"Failed to start bot: {exc}")


if __name__ == "__main__":
    ensure_token()
    bot.run(TOKEN)
