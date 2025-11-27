import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- 環境変数読み込み ---
load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
JST = timezone(timedelta(hours=9))


def read_int_env(name: str) -> Optional[int]:
    value = os.getenv(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


REMINDER_MINUTES_BEFORE = read_int_env("REMINDER_MINUTES_BEFORE") or 0
REMINDER_CHANNEL_ID = read_int_env("REMINDER_CHANNEL_ID") or 0


def load_google_credentials() -> service_account.Credentials:
    """GOOGLE_CREDENTIALS_JSON もしくは GOOGLE_CREDENTIALS_PATH を基に認証情報を構築する。"""
    json_blob = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if json_blob:
        info = json.loads(json_blob)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    path = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
    if not os.path.exists(path):
        raise RuntimeError("Google認証情報が見つかりません。GOOGLE_CREDENTIALS_JSON もしくは GOOGLE_CREDENTIALS_PATH を設定してください。")

    return service_account.Credentials.from_service_account_file(path, scopes=SCOPES)


def normalize_date(text: str) -> str:
    value = text.strip()
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.strftime("%Y/%m/%d")
        except ValueError:
            continue
    raise ValueError("日付は YYYY/MM/DD 形式で入力してください。")


def normalize_time(text: str) -> str:
    value = text.strip()
    for fmt in ("%H:%M", "%H%M"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.strftime("%H:%M")
        except ValueError:
            continue
    raise ValueError("時間は HH:MM 形式で入力してください。")


def time_to_minutes(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


def has_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    sa = time_to_minutes(start_a)
    ea = time_to_minutes(end_a)
    sb = time_to_minutes(start_b)
    eb = time_to_minutes(end_b)
    return max(sa, sb) < min(ea, eb)


class SheetClient:
    """Google Sheets とのやり取りをカプセル化。"""

    def __init__(self, spreadsheet_id: str, sheet_name: str, credentials: service_account.Credentials):
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        self.service = build("sheets", "v4", credentials=credentials)
        self.api = self.service.spreadsheets()
        self.sheet_id = self._ensure_sheet_exists()
        self.header = [
            "予約者",
            "席名",
            "日付",
            "開始",
            "終了",
            "ユーザーID",
            "タイムスタンプ",
            "参加者JSON",
            "reminded",
        ]
        self._ensure_header_row()

    def _ensure_sheet_exists(self) -> int:
        response = self.api.get(spreadsheetId=self.spreadsheet_id).execute()
        sheets = response.get("sheets", [])
        for sheet in sheets:
            prop = sheet.get("properties", {})
            if prop.get("title") == self.sheet_name:
                sheet_id = prop.get("sheetId")
                column_count = prop.get("gridProperties", {}).get("columnCount", 0)
                if column_count < 9:
                    self._ensure_min_columns(sheet_id, 9)
                return sheet_id

        request = {"requests": [{"addSheet": {"properties": {"title": self.sheet_name}}}]}
        result = self.api.batchUpdate(spreadsheetId=self.spreadsheet_id, body=request).execute()
        sheet_id = result["replies"][0]["addSheet"]["properties"]["sheetId"]
        self._ensure_min_columns(sheet_id, 9)
        return sheet_id

    def _ensure_min_columns(self, sheet_id: int, min_columns: int) -> None:
        update_body = {
            "requests": [
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "gridProperties": {"columnCount": min_columns},
                        },
                        "fields": "gridProperties.columnCount",
                    }
                }
            ]
        }
        self.api.batchUpdate(spreadsheetId=self.spreadsheet_id, body=update_body).execute()

    def _ensure_header_row(self) -> None:
        range_a1 = f"{self.sheet_name}!A1:I1"
        result = self.api.values().get(spreadsheetId=self.spreadsheet_id, range=range_a1).execute()
        values = result.get("values", [])
        if not values or values[0] != self.header:
            self.api.values().update(
                spreadsheetId=self.spreadsheet_id,
                range=range_a1,
                valueInputOption="RAW",
                body={"values": [self.header]},
            ).execute()

    def fetch_rows(self) -> List[Tuple[int, List[str]]]:
        range_a1 = f"{self.sheet_name}!A:I"
        result = self.api.values().get(spreadsheetId=self.spreadsheet_id, range=range_a1).execute()
        rows = result.get("values", [])
        output: List[Tuple[int, List[str]]] = []
        for idx, row in enumerate(rows, start=1):
            if idx == 1:
                continue
            padded = row + [""] * max(0, 9 - len(row))
            output.append((idx, padded[:9]))
        return output

    def conflicting_seat_names(self, day: str, start: str, end: str) -> List[str]:
        conflicts = []
        for _, row in self.fetch_rows():
            row_day = row[2]
            row_start = row[3]
            row_end = row[4]
            if not row_day or not row_start or not row_end:
                continue
            if row_day != day:
                continue
            if has_overlap(start, end, row_start, row_end):
                conflicts.append(row[1])
        return conflicts

    def append_reservation(
        self,
        user_display: str,
        channel_name: str,
        day: str,
        start: str,
        end: str,
        user_id: int,
    ) -> int:
        timestamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        values = [
            user_display,
            channel_name,
            day,
            start,
            end,
            str(user_id),
            timestamp,
            "[]",
            "FALSE",
        ]
        response = (
            self.api.values()
            .append(
                spreadsheetId=self.spreadsheet_id,
                range=f"{self.sheet_name}!A:I",
                valueInputOption="USER_ENTERED",
                body={"values": [values]},
            )
            .execute()
        )
        updated = response.get("updates", {})
        updated_range = updated.get("updatedRange", "")
        # updatedRange 例: "sheet1!A5:H5"
        row_number = 0
        try:
            row_part = updated_range.split("!")[1]
            row_number = int(row_part.split(":")[0][1:])
        except Exception:
            pass
        return row_number

    def update_assistants(self, row_index: int, assistant_ids: Sequence[int]) -> None:
        payload = json.dumps([str(user_id) for user_id in assistant_ids], ensure_ascii=False)
        target_range = f"{self.sheet_name}!H{row_index}"
        self.api.values().update(
            spreadsheetId=self.spreadsheet_id,
            range=target_range,
            valueInputOption="RAW",
            body={"values": [[payload]]},
        ).execute()

    def mark_reminded(self, row_index: int) -> None:
        target_range = f"{self.sheet_name}!I{row_index}"
        self.api.values().update(
            spreadsheetId=self.spreadsheet_id,
            range=target_range,
            valueInputOption="RAW",
            body={"values": [["TRUE"]]},
        ).execute()

    def find_matching_row(
        self,
        *,
        user_id: int,
        channel_name: str,
        day: str,
        start: str,
        end: str,
    ) -> Optional[int]:
        key = (str(user_id), channel_name, day, start, end)
        for index, row in self.fetch_rows():
            row_key = (row[5], row[1], row[2], row[3], row[4])
            if row_key == key:
                return index
        return None

    def delete_row(self, row_index: int) -> None:
        body = {
            "requests": [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": self.sheet_id,
                            "dimension": "ROWS",
                            "startIndex": row_index - 1,
                            "endIndex": row_index,
                        }
                    }
                }
            ]
        }
        self.api.batchUpdate(spreadsheetId=self.spreadsheet_id, body=body).execute()

    def recent_reservations(self, limit: int = 10) -> List[Dict[str, str]]:
        rows = self.fetch_rows()
        # 末尾ほど新しいので後ろから拾う
        recent = rows[-limit:]
        output: List[Dict[str, str]] = []
        for _, row in recent:
            data = {
                "user": row[0],
                "channel": row[1],
                "day": row[2],
                "start": row[3],
                "end": row[4],
                "user_id": row[5],
                "timestamp": row[6],
                "assistants": row[7],
                "reminded": row[8],
            }
            output.append(data)
        return list(reversed(output))


def get_category_voice_channels(guild: Optional[discord.Guild], category_id: int) -> List[discord.VoiceChannel]:
    """指定カテゴリ配下のボイスチャンネル一覧を返す。"""
    if not guild:
        return []
    category = guild.get_channel(category_id)
    if not category or not isinstance(category, discord.CategoryChannel):
        return []
    return [channel for channel in category.channels if isinstance(channel, discord.VoiceChannel)]


class ReservationModal(discord.ui.Modal, title="カフェ予約"):
    def __init__(self, sheet: SheetClient, category_id: int):
        super().__init__(timeout=300)
        self.sheet = sheet
        self.category_id = category_id
        self.date_input = discord.ui.TextInput(label="日付 (YYYY/MM/DD)", default="2025/01/20", required=True, max_length=10)
        self.start_input = discord.ui.TextInput(label="開始 (HH:MM)", default="13:00", required=True, max_length=5)
        self.end_input = discord.ui.TextInput(label="終了 (HH:MM)", default="14:00", required=True, max_length=5)
        self.add_item(self.date_input)
        self.add_item(self.start_input)
        self.add_item(self.end_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            day = normalize_date(self.date_input.value)
            start = normalize_time(self.start_input.value)
            end = normalize_time(self.end_input.value)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        if time_to_minutes(start) >= time_to_minutes(end):
            await interaction.response.send_message("開始時間は終了時間より前にしてください。", ephemeral=True)
            return

        category = interaction.guild.get_channel(self.category_id) if interaction.guild else None
        if not category or not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("カフェ用カテゴリが見つかりません。", ephemeral=True)
            return

        seat_channels = [
            channel for channel in category.channels if isinstance(channel, discord.VoiceChannel)
        ]
        if not seat_channels:
            await interaction.response.send_message("予約対象のVCがカテゴリ内に存在しません。", ephemeral=True)
            return

        conflicts = set(self.sheet.conflicting_seat_names(day, start, end))
        available_channels = [ch for ch in seat_channels if ch.name not in conflicts]
        if not available_channels:
            await interaction.response.send_message("指定した時間帯で空いている席がありません。", ephemeral=True)
            return

        view = ReservationSeatSelectView(
            sheet=self.sheet,
            channels=available_channels,
            day=day,
            start=start,
            end=end,
            user=interaction.user,
        )
        await interaction.response.send_message(
            f"{day} {start}〜{end} に利用する席を選択してください。",
            view=view,
            ephemeral=True,
        )


class ReservationSeatSelect(discord.ui.Select):
    def __init__(self, parent: "ReservationSeatSelectView"):
        options = [
            discord.SelectOption(label=channel.name, value=str(channel.id)) for channel in parent.channels
        ]
        super().__init__(
            placeholder="利用する席を選択",
            min_values=1,
            max_values=1,
            options=options[:25],
            custom_id="cafebook:seat_select",
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.parent_view.user.id:
            await interaction.response.send_message("自分の予約リクエストのみ操作できます。", ephemeral=True)
            return

        channel_id = int(self.values[0])
        channel = discord.utils.get(self.parent_view.channels, id=channel_id)
        if not channel:
            await interaction.response.send_message("選択した席が見つかりません。", ephemeral=True)
            return

        row_index = self.parent_view.sheet.append_reservation(
            user_display=interaction.user.display_name,
            channel_name=channel.name,
            day=self.parent_view.day,
            start=self.parent_view.start,
            end=self.parent_view.end,
            user_id=interaction.user.id,
        )

        assistants_view = AssistantPromptView(sheet=self.parent_view.sheet, row_index=row_index, owner_id=interaction.user.id)
        message = (
            f"✅ 予約を登録しました。\n"
            f"・席: **{channel.name}**\n"
            f"・日付: **{self.parent_view.day}**\n"
            f"・時間: **{self.parent_view.start}〜{self.parent_view.end}**"
        )
        await interaction.response.edit_message(content=message, view=assistants_view)


class ReservationSeatSelectView(discord.ui.View):
    def __init__(self, sheet: SheetClient, channels: Sequence[discord.VoiceChannel], day: str, start: str, end: str, user: discord.abc.User):
        super().__init__(timeout=180)
        self.sheet = sheet
        self.channels = list(channels)
        self.day = day
        self.start = start
        self.end = end
        self.user = user
        self.add_item(ReservationSeatSelect(self))


class AssistantPromptView(discord.ui.View):
    def __init__(self, sheet: SheetClient, row_index: int, owner_id: int):
        super().__init__(timeout=180)
        self.sheet = sheet
        self.row_index = row_index
        self.owner_id = owner_id

    @discord.ui.button(label="👥 参加者を追加", style=discord.ButtonStyle.success, custom_id="cafebook:add_assistants")
    async def add_assistants(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("予約者のみ参加者を追加できます。", ephemeral=True)
            return

        view = AssistantSelectView(sheet=self.sheet, row_index=self.row_index, owner_id=self.owner_id)
        await interaction.response.send_message("一緒に参加するメンバーを選択してください。", view=view, ephemeral=True)

    @discord.ui.button(label="スキップ", style=discord.ButtonStyle.secondary, custom_id="cafebook:skip_assistants")
    async def skip_assistants(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("予約者のみ操作できます。", ephemeral=True)
            return
        await interaction.response.edit_message(content="予約を保存しました。", view=None)


class AssistantSelect(discord.ui.UserSelect):
    def __init__(self, parent: "AssistantSelectView"):
        super().__init__(
            placeholder="参加者を選択 (最大25人)",
            min_values=1,
            max_values=25,
            custom_id="cafebook:assistant_select",
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.parent_view.owner_id:
            await interaction.response.send_message("予約者のみ参加者を登録できます。", ephemeral=True)
            return

        assistant_ids = [member.id for member in self.values]
        self.parent_view.sheet.update_assistants(self.parent_view.row_index, assistant_ids)
        mentions = ", ".join(member.mention for member in self.values)
        await interaction.response.send_message(f"参加者として {mentions} を登録しました。", ephemeral=True)


class AssistantSelectView(discord.ui.View):
    def __init__(self, sheet: SheetClient, row_index: int, owner_id: int):
        super().__init__(timeout=180)
        self.sheet = sheet
        self.row_index = row_index
        self.owner_id = owner_id
        self.add_item(AssistantSelect(self))


class CancelSeatSelect(discord.ui.Select):
    def __init__(self, parent: "CancelSeatView", channels: Sequence[discord.VoiceChannel]):
        options = [
            discord.SelectOption(label=channel.name, value=str(channel.id)) for channel in channels
        ]
        super().__init__(placeholder="キャンセルする席を選択", min_values=1, max_values=1, options=options[:25], custom_id="cafebook:cancel_seat")
        self.parent_view = parent
        self.channels = {str(channel.id): channel for channel in channels}

    async def callback(self, interaction: discord.Interaction) -> None:
        channel = self.channels.get(self.values[0])
        if not channel:
            await interaction.response.send_message("選択した席が見つかりません。", ephemeral=True)
            return

        modal = CancelReservationModal(
            sheet=self.parent_view.sheet,
            channel_name=channel.name,
            owner_id=interaction.user.id,
        )
        await interaction.response.send_modal(modal)


class CancelSeatView(discord.ui.View):
    def __init__(self, sheet: SheetClient, channels: Sequence[discord.VoiceChannel]):
        super().__init__(timeout=120)
        self.sheet = sheet
        self.add_item(CancelSeatSelect(self, channels))


class CancelReservationModal(discord.ui.Modal, title="予約キャンセル"):
    def __init__(self, sheet: SheetClient, channel_name: str, owner_id: int):
        super().__init__(timeout=180)
        self.sheet = sheet
        self.channel_name = channel_name
        self.owner_id = owner_id

        self.date_input = discord.ui.TextInput(label="日付 (YYYY/MM/DD)", default="2025/01/20")
        self.start_input = discord.ui.TextInput(label="開始 (HH:MM)", default="13:00")
        self.end_input = discord.ui.TextInput(label="終了 (HH:MM)", default="14:00")
        self.add_item(self.date_input)
        self.add_item(self.start_input)
        self.add_item(self.end_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            day = normalize_date(self.date_input.value)
            start = normalize_time(self.start_input.value)
            end = normalize_time(self.end_input.value)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        row_index = self.sheet.find_matching_row(
            user_id=interaction.user.id,
            channel_name=self.channel_name,
            day=day,
            start=start,
            end=end,
        )
        if not row_index:
            await interaction.response.send_message("一致する予約が見つかりません。入力内容をご確認ください。", ephemeral=True)
            return

        self.sheet.delete_row(row_index)
        await interaction.response.send_message("予約をキャンセルしました。", ephemeral=True)


class ReservationPanelView(discord.ui.View):
    def __init__(self, sheet: SheetClient, category_id: int):
        super().__init__(timeout=None)
        self.sheet = sheet
        self.category_id = category_id

    @discord.ui.button(label="予約", style=discord.ButtonStyle.primary, custom_id="cafebook:reserve_main")
    async def handle_reserve(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        modal = ReservationModal(sheet=self.sheet, category_id=self.category_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.danger, custom_id="cafebook:cancel_main")
    async def handle_cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        category = interaction.guild.get_channel(self.category_id) if interaction.guild else None
        if not category or not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("カフェ用カテゴリが見つかりません。", ephemeral=True)
            return
        seat_channels = [ch for ch in category.channels if isinstance(ch, discord.VoiceChannel)]
        if not seat_channels:
            await interaction.response.send_message("キャンセル対象の席がありません。", ephemeral=True)
            return
        await interaction.response.send_message(
            "キャンセルする席を選択してください。",
            view=CancelSeatView(sheet=self.sheet, channels=seat_channels),
            ephemeral=True,
        )

    @discord.ui.button(label="予約確認", style=discord.ButtonStyle.secondary, custom_id="cafebook:confirm_main")
    async def handle_confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        records = self.sheet.recent_reservations(limit=10)
        if not records:
            await interaction.response.send_message("まだ予約が登録されていません。", ephemeral=True)
            return

        embed = discord.Embed(title="最新の予約リスト", color=discord.Color.blurple())
        for record in records:
            assistants_display = ""
            raw = record.get("assistants") or "[]"
            try:
                ids = json.loads(raw)
            except json.JSONDecodeError:
                ids = []
            mentions = "、".join(f"<@{user_id}>" for user_id in ids) if ids else "なし"
            field_name = f"{record['day']} {record['start']}〜{record['end']} / {record['channel']}"
            field_value = (
                f"予約者: {record['user']} (<@{record['user_id']}>)\n"
                f"参加者: {mentions}\n"
                f"登録: {record['timestamp']}"
            )
            embed.add_field(name=field_name, value=field_value, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


def resolve_ids() -> Tuple[int, Optional[int], Optional[discord.Object], int]:
    test_mode = os.getenv("TEST_SERVER", "false").lower() == "true"

    def to_int(value: Optional[str]) -> Optional[int]:
        return int(value) if value and value.isdigit() else None

    if test_mode:
        cafe_id = to_int(os.getenv("CAFE_CATEGORY_ID_TEST"))
        guild_id = to_int(os.getenv("GUILD_ID_TEST"))
        reminder_channel_id = to_int(os.getenv("REMINDER_CHANNEL_ID_TEST")) or read_int_env("REMINDER_CHANNEL_ID_TEST") or 0
    else:
        cafe_id = to_int(os.getenv("CAFE_CATEGORY_ID"))
        guild_id = to_int(os.getenv("GUILD_ID"))
        reminder_channel_id = read_int_env("REMINDER_CHANNEL_ID") or 0
    if not cafe_id:
        raise RuntimeError("CAFE_CATEGORY_ID (または CAFE_CATEGORY_ID_TEST) が設定されていません。")
    guild_object = discord.Object(id=guild_id) if guild_id else None
    return cafe_id, guild_id, guild_object, reminder_channel_id


TOKEN = os.getenv("DISCORD_TOKEN")
SPREADSHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "sheet1")

if not TOKEN or not SPREADSHEET_ID:
    raise RuntimeError("DISCORD_TOKEN と GOOGLE_SHEET_ID を設定してください。")

CREDENTIALS = load_google_credentials()
CAFE_CATEGORY_ID, GUILD_ID_VALUE, GUILD_OBJ, REMINDER_CHANNEL_ID = resolve_ids()
SHEET_CLIENT = SheetClient(SPREADSHEET_ID, SHEET_NAME, CREDENTIALS)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

_reminder_channel_cache: Optional[discord.abc.Messageable] = None


async def _get_reminder_channel() -> Optional[discord.abc.Messageable]:
    global _reminder_channel_cache
    if REMINDER_CHANNEL_ID <= 0:
        return None
    if _reminder_channel_cache:
        return _reminder_channel_cache
    channel = bot.get_channel(REMINDER_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(REMINDER_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
    _reminder_channel_cache = channel
    return channel


def _parse_user_ids(raw_value: str) -> List[int]:
    ids: List[int] = []
    if not raw_value:
        return ids
    try:
        ids.append(int(raw_value))
    except (TypeError, ValueError):
        pass
    return ids


def _parse_assistant_ids(raw_json: str) -> List[int]:
    if not raw_json:
        return []
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        return []
    results: List[int] = []
    for item in parsed:
        try:
            results.append(int(item))
        except (TypeError, ValueError):
            continue
    return results


@tasks.loop(minutes=1)
async def reminder_loop():
    if REMINDER_MINUTES_BEFORE <= 0 or REMINDER_CHANNEL_ID <= 0:
        return

    channel = await _get_reminder_channel()
    if channel is None:
        return

    now_key = datetime.now(JST).replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")
    reminder_delta = timedelta(minutes=REMINDER_MINUTES_BEFORE)
    guild = bot.get_guild(GUILD_ID_VALUE) if GUILD_ID_VALUE else None
    valid_voice_names = {vc.name for vc in get_category_voice_channels(guild, CAFE_CATEGORY_ID)} if guild else set()
    pending_notifications: List[Tuple[int, List[str]]] = []

    for row_index, row in SHEET_CLIENT.fetch_rows():
        reminded_flag = (row[8] or "").strip().lower() == "true"
        if reminded_flag:
            continue
        day = (row[2] or "").strip()
        start = (row[3] or "").strip()
        if not day or not start:
            continue
        try:
            start_dt = datetime.strptime(f"{day} {start}", "%Y/%m/%d %H:%M").replace(tzinfo=JST)
        except ValueError:
            continue
        reminder_dt = start_dt - reminder_delta
        if reminder_dt.strftime("%Y-%m-%d %H:%M") != now_key:
            continue
        if valid_voice_names and row[1] not in valid_voice_names:
            continue
        pending_notifications.append((row_index, row))

    for row_index, row in pending_notifications:
        mention_ids = set(_parse_user_ids(row[5]))
        for assistant_id in _parse_assistant_ids(row[7]):
            mention_ids.add(assistant_id)

        mention_text = " ".join(f"<@{user_id}>" for user_id in mention_ids).strip()
        reminder_phrase = f"あと{REMINDER_MINUTES_BEFORE}分で" if REMINDER_MINUTES_BEFORE > 0 else "まもなく"
        message_lines = [
            mention_text,
            f"{reminder_phrase} {row[2]} {row[3]}〜{row[4] or '未設定'} のカフェ予約（{row[1]}）が始まります。",
        ]
        message = "\n".join([line for line in message_lines if line])
        try:
            await channel.send(message)
        except discord.HTTPException:
            continue
        SHEET_CLIENT.mark_reminded(row_index)


@reminder_loop.before_loop
async def before_reminder_loop():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    try:
        if GUILD_OBJ:
            await bot.tree.sync(guild=GUILD_OBJ)
        else:
            await bot.tree.sync()
        bot.add_view(ReservationPanelView(sheet=SHEET_CLIENT, category_id=CAFE_CATEGORY_ID))
        if not reminder_loop.is_running():
            reminder_loop.start()
        print(f"✅ cafebook bot ready as {bot.user} (guild scope: {GUILD_ID_VALUE})")
    except Exception as exc:
        print(f"Failed to sync commands: {exc}")


def _maybe_guild_scope(func):
    if GUILD_OBJ:
        return app_commands.guilds(GUILD_OBJ)(func)
    return func


@bot.tree.command(name="cafebook_panel", description="カフェ予約ボタンを配置します。")
@_maybe_guild_scope
async def cafebook_panel(interaction: discord.Interaction):
    view = ReservationPanelView(sheet=SHEET_CLIENT, category_id=CAFE_CATEGORY_ID)
    await interaction.response.send_message("カフェ予約パネルを設置しました。", view=view)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.content.strip() == "カフェ予約":
        view = ReservationPanelView(sheet=SHEET_CLIENT, category_id=CAFE_CATEGORY_ID)
        await message.channel.send("カフェ予約パネルを表示します。", view=view)
        return
    await bot.process_commands(message)


if __name__ == "__main__":
    bot.run(TOKEN)
