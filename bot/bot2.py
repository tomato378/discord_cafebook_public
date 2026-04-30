import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import discord
from aiohttp import web
from discord import app_commands, ui
from discord.ext import commands, tasks
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- ���ϐ� ---
_env_path = Path(__file__).resolve().parents[1] / ".env"
if _env_path.exists():
    load_dotenv(_env_path)
else:
    load_dotenv()


def _read_int(name: str) -> Optional[int]:
    value = os.getenv(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _read_first_int(*names: str) -> Optional[int]:
    for name in names:
        value = os.getenv(name)
        if value is None:
            continue
        try:
            return int(value)
        except ValueError:
            continue
    return None


def _read_first_str(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is None:
            continue
        stripped = value.strip()
        if stripped:
            return stripped
    return default


def _parse_run_mode(argv: Optional[Sequence[str]] = None) -> str:
    parser = argparse.ArgumentParser(description="CafeBook Discord Bot")
    parser.add_argument("--mode", choices=["prod", "test"], default="prod")
    args, _ = parser.parse_known_args(argv)
    return args.mode


RUN_MODE = _parse_run_mode(sys.argv[1:])
IS_TEST_MODE = RUN_MODE == "test"

TOKEN = os.getenv("DISCORD_TOKEN")
SPREADSHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "sheet1")
RESERVE_SHEET_NAME = os.getenv("RESERVE_SHEET_NAME", "reserve")
CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
BOT_TEST_USER_ID = _read_int("BOT_TEST_USER_ID")


@dataclass(frozen=True)
class ModeConfig:
    mode: str
    guild_id: Optional[int]
    cafe_category_id: int
    cafe_category_name: str
    reservation_announce_channel_id: int
    reminder_channel_id: int
    reminder_minutes_before: int


def _load_mode_config(mode: str) -> ModeConfig:
    if mode == "test":
        guild_id = _read_first_int("TEST_GUILD_ID", "GUILD_ID_TEST")
        cafe_category_id = _read_first_int("TEST_CAFE_CATEGORY_ID", "CAFE_CATEGORY_ID_TEST") or 0
        cafe_category_name = _read_first_str(
            "TEST_CAFE_CATEGORY_NAME",
            "CAFE_CATEGORY_NAME_TEST",
            default="�J�t�F",
        )
        reservation_announce_channel_id = (
            _read_first_int("TEST_RESERVATION_ANNOUNCE_CHANNEL_ID", "RESERVATION_ANNOUNCE_CHANNEL_ID_TEST")
            or 0
        )
        reminder_channel_id = (
            _read_first_int("TEST_REMINDER_CHANNEL_ID", "REMINDER_CHANNEL_ID_TEST") or 0
        )
        reminder_minutes_before = (
            _read_first_int("TEST_REMINDER_MINUTES_BEFORE", "REMINDER_MINUTES_BEFORE_TEST")
            or _read_first_int("REMINDER_MINUTES_BEFORE")
            or 15
        )
        return ModeConfig(
            mode=mode,
            guild_id=guild_id,
            cafe_category_id=cafe_category_id,
            cafe_category_name=cafe_category_name,
            reservation_announce_channel_id=reservation_announce_channel_id,
            reminder_channel_id=reminder_channel_id,
            reminder_minutes_before=reminder_minutes_before,
        )

    guild_id = _read_first_int("PROD_GUILD_ID", "GUILD_ID")
    cafe_category_id = _read_first_int("PROD_CAFE_CATEGORY_ID", "CAFE_CATEGORY_ID") or 0
    cafe_category_name = _read_first_str("PROD_CAFE_CATEGORY_NAME", "CAFE_CATEGORY_NAME")
    reservation_announce_channel_id = (
        _read_first_int("PROD_RESERVATION_ANNOUNCE_CHANNEL_ID", "RESERVATION_ANNOUNCE_CHANNEL_ID")
        or 0
    )
    reminder_channel_id = _read_first_int("PROD_REMINDER_CHANNEL_ID", "REMINDER_CHANNEL_ID") or 0
    reminder_minutes_before = (
        _read_first_int("PROD_REMINDER_MINUTES_BEFORE", "REMINDER_MINUTES_BEFORE") or 15
    )
    return ModeConfig(
        mode=mode,
        guild_id=guild_id,
        cafe_category_id=cafe_category_id,
        cafe_category_name=cafe_category_name,
        reservation_announce_channel_id=reservation_announce_channel_id,
        reminder_channel_id=reminder_channel_id,
        reminder_minutes_before=reminder_minutes_before,
    )


MODE_CONFIG = _load_mode_config(RUN_MODE)
GUILD_OBJ = (
    discord.Object(id=MODE_CONFIG.guild_id)
    if IS_TEST_MODE and MODE_CONFIG.guild_id
    else None
)
JST = timezone(timedelta(hours=9))


# --- Bot �ݒ� ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)
_health_app_started = False


# --- ���[�e�B���e�B ---

def _maybe_guild_scope(func):
    if IS_TEST_MODE and GUILD_OBJ:
        return app_commands.guilds(GUILD_OBJ)(func)
    return func


def _mode_prefix() -> str:
    return "[TEST] " if IS_TEST_MODE else ""


def _category_hint(guild: Optional[discord.Guild]) -> str:
    names = [cat.name for cat in guild.categories] if guild else []
    return (
        "�J�e�S����������܂���B\n"
        f"�ݒ�ID: {MODE_CONFIG.cafe_category_id or '���ݒ�'} / "
        f"�ݒ�NAME: {MODE_CONFIG.cafe_category_name or '���ݒ�'}\n"
        f"�M���h�̃J�e�S���ꗗ: {', '.join(names) if names else '�擾�ł��܂���ł���'}"
    )


def ensure_token() -> None:
    if not TOKEN or not SPREADSHEET_ID:
        raise RuntimeError("DISCORD_TOKEN �� GOOGLE_SHEET_ID ��ݒ肵�Ă�������")
    if MODE_CONFIG.cafe_category_id <= 0 and not MODE_CONFIG.cafe_category_name:
        raise RuntimeError(
            "CAFE_CATEGORY_ID/NAME ��ݒ肵�Ă������� (PROD_/TEST_ �̂����ꂩ)"
        )
    load_credentials()


async def resolve_cafe_category(
    bot_client: commands.Bot,
    guild: Optional[discord.Guild],
    config: ModeConfig,
) -> Optional[discord.CategoryChannel]:
    if not guild:
        return None
    if config.cafe_category_id:
        ch = guild.get_channel(config.cafe_category_id)
        if isinstance(ch, discord.CategoryChannel):
            return ch
        try:
            fetched = await bot_client.fetch_channel(config.cafe_category_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            fetched = None
        if isinstance(fetched, discord.CategoryChannel) and fetched.guild.id == guild.id:
            return fetched
    if config.cafe_category_name:
        for cat in guild.categories:
            if cat.name == config.cafe_category_name:
                return cat
        lowered = config.cafe_category_name.lower()
        for cat in guild.categories:
            if lowered in cat.name.lower():
                return cat
    return None


def parse_time(text: str) -> datetime.time:
    return datetime.strptime(text, "%H:%M").time()


def overlaps(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    sa = parse_time(start_a)
    ea = parse_time(end_a)
    sb = parse_time(start_b)
    eb = parse_time(end_b)
    return max(sa, sb) < min(ea, eb)


def is_past_reservation(day: str, end: str) -> bool:
    try:
        end_dt = datetime.strptime(f"{day} {end}", "%Y/%m/%d %H:%M").replace(tzinfo=JST)
    except ValueError:
        return False
    return end_dt < datetime.now(JST)


def load_credentials():
    json_blob = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if json_blob:
        info = json.loads(json_blob)
        return service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )

    explicit_path = os.getenv("GOOGLE_CREDENTIALS_PATH")
    if explicit_path and os.path.exists(explicit_path):
        return service_account.Credentials.from_service_account_file(
            explicit_path,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )

    secret_file_path = "/etc/secrets/credentials.json"
    if os.path.exists(secret_file_path):
        return service_account.Credentials.from_service_account_file(
            secret_file_path,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )

    local_path = "credentials.json"
    if os.path.exists(local_path):
        return service_account.Credentials.from_service_account_file(
            local_path,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )

    raise RuntimeError("Google �F�؏�񂪌�����܂���")


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
    print(f"?? Health server running on 0.0.0.0:{port}")

# --- Google Sheet ���� ---
class SheetOperations:
    def __init__(self) -> None:
        self.service = None
        self.sheet_name = SHEET_NAME
        self.header = [
            "�\���",
            "�`�����l��",
            "���t",
            "�J�n",
            "�I��",
            "�\���ID",
            "�Q����JSON",
            "�쐬����",
            "reminded",
        ]
        self.sheet_id: Optional[int] = None
        self._header_checked = False

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
        self.sheet_id = (
            info.get("sheets", [{}])[0].get("properties", {}).get("sheetId", 0)
        )
        return self.sheet_id

    def ensure_header_row(self) -> None:
        if self._header_checked:
            return
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
            self._header_checked = True
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
        self._header_checked = True

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

    def update_participants(
        self, row_index: int, participants: Sequence[Dict[str, str]]
    ) -> None:
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

    def is_slot_available_from_rows(
        self,
        rows: List[Tuple[int, List[str]]],
        channel_name: str,
        day: str,
        start: str,
        end: str,
    ) -> bool:
        for _, row in rows:
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


async def sheets_call(func, *args, **kwargs):
    """������Google Sheets�Ăяo����ʃX���b�h�Ŏ��s����"""
    return await asyncio.to_thread(func, *args, **kwargs)

# --- Simple reserve logging (issue #1) ---
class SimpleReserveSheet:
    def __init__(self) -> None:
        self.service = None
        self.sheet_name = RESERVE_SHEET_NAME
        self.header = ["user", "item", "time", "timestamp", "user_id"]
        self.sheet_id: Optional[int] = None

    def _get_api(self):
        if not self.service:
            creds = load_credentials()
            self.service = build("sheets", "v4", credentials=creds).spreadsheets()
        return self.service

    def _ensure_sheet(self) -> int:
        if self.sheet_id is not None:
            return self.sheet_id
        api = self._get_api()
        info = api.get(spreadsheetId=SPREADSHEET_ID).execute()
        for sheet in info.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == self.sheet_name:
                self.sheet_id = props.get("sheetId", 0)
                return self.sheet_id
        response = api.batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": self.sheet_name}}}]},
        ).execute()
        replies = response.get("replies", [])
        self.sheet_id = (
            replies[0].get("addSheet", {}).get("properties", {}).get("sheetId", 0)
            if replies
            else 0
        )
        return self.sheet_id

    def ensure_header_row(self) -> None:
        self._ensure_sheet()
        api = self._get_api()
        result = api.values().get(
            spreadsheetId=SPREADSHEET_ID, range=f"{self.sheet_name}!A1:E1"
        ).execute()
        values = result.get("values", [])
        if values and values[0] == self.header:
            return
        api.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{self.sheet_name}!A1:E1",
            valueInputOption="RAW",
            body={"values": [self.header]},
        ).execute()

    def append_row(self, user_mention: str, item: str, time_text: str, user_id: int) -> None:
        self.ensure_header_row()
        api = self._get_api()
        timestamp = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
        api.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{self.sheet_name}!A:E",
            valueInputOption="USER_ENTERED",
            body={"values": [[user_mention, item, time_text, timestamp, str(user_id)]]},
        ).execute()


reserve_sheet = SimpleReserveSheet()


# --- UI �R���|�[�l���g ---
class TimeInputModal(ui.Modal, title="? �\�񎞊Ԃ����"):
    def __init__(self, user: discord.User):
        super().__init__(timeout=300)
        self.request_user = user
        self.day = ui.TextInput(
            label="���t(YYYY/MM/DD)",
            default=datetime.now(JST).strftime("%Y/%m/%d"),
        )
        self.start_time = ui.TextInput(label="�J�n(HH:MM)", default="13:00")
        self.end_time = ui.TextInput(label="�I��(HH:MM)", default="14:00")
        self.add_item(self.day)
        self.add_item(self.start_time)
        self.add_item(self.end_time)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            datetime.strptime(self.day.value, "%Y/%m/%d")
            start_t = parse_time(self.start_time.value)
            end_t = parse_time(self.end_time.value)
        except ValueError:
            await interaction.followup.send(
                "���t�܂��͎��Ԃ̌`��������������܂���", ephemeral=True
            )
            return

        if start_t >= end_t:
            await interaction.followup.send(
                "�J�n���Ԃ��I�����Ԃ��O�ɂ��Ă�������", ephemeral=True
            )
            return

        category = await resolve_cafe_category(bot, interaction.guild, MODE_CONFIG)
        if not category or not isinstance(category, discord.CategoryChannel):
            await interaction.followup.send(
                _category_hint(interaction.guild), ephemeral=True
            )
            return

        candidates = [
            ch for ch in category.channels if isinstance(ch, discord.VoiceChannel)
        ]
        rows = await sheets_call(sheets.fetch_rows)
        available = [
            ch for ch in candidates
            if sheets.is_slot_available_from_rows(
                rows, ch.name, self.day.value, self.start_time.value, self.end_time.value
            )
        ]

        if not available:
            await interaction.followup.send(
                "�w�莞�Ԃɋ󂢂Ă���Ȃ�����܂���", ephemeral=True
            )
            return

        view = ChannelSelectView(
            user=interaction.user,
            channels=available,
            day=self.day.value,
            start=self.start_time.value,
            end=self.end_time.value,
        )
        await interaction.followup.send(
            f"{self.day.value} {self.start_time.value}?{self.end_time.value} �ŗ\�񂷂�Ȃ�I��ł�������",
            view=view,
            ephemeral=True,
        )


class ChannelSelect(ui.Select):
    def __init__(self, parent: "ChannelSelectView"):
        options = [
            discord.SelectOption(label=ch.name, value=str(ch.id))
            for ch in parent.channels
        ]
        super().__init__(
            placeholder="�Ȃ�I��",
            min_values=1,
            max_values=1,
            options=options[:25],
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent_view.user.id:
            await interaction.response.send_message(
                "�\��҂̂ݑ���ł��܂�", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        channel_id = int(self.values[0])
        channel = discord.utils.get(self.parent_view.channels, id=channel_id)
        if not channel:
            await interaction.followup.send("�`�����l����������܂���", ephemeral=True)
            return

        row_index = await sheets_call(
            sheets.append_row,
            interaction.user.mention,
            channel.name,
            self.parent_view.day,
            self.parent_view.start,
            self.parent_view.end,
            interaction.user.id,
        )

        announce_channel = (
            interaction.guild.get_channel(MODE_CONFIG.reservation_announce_channel_id)
            if interaction.guild
            else None
        )
        participant_view = ParticipantSelectView(
            row_index=row_index,
            owner=interaction.user,
            channel_name=channel.name,
            day=self.parent_view.day,
            start=self.parent_view.start,
            end=self.parent_view.end,
            announce_channel=announce_channel,
            user_mention=interaction.user.mention,
        )
        await interaction.followup.send(
            content=(
                "�\���o�^���܂����B\n"
                f"��: {channel.name}\n"
                f"���t: {self.parent_view.day}\n"
                f"����: {self.parent_view.start}?{self.parent_view.end}\n"
                "�Q���҂�ǉ����܂����H�i�C�ӁE�X�L�b�v�j"
            ),
            view=participant_view,
            ephemeral=True,
        )
        if (
            MODE_CONFIG.reservation_announce_channel_id
            and participant_view.announce_channel is None
        ):
            await interaction.followup.send(
                "�\��A�i�E���X�`�����l����������܂���ł���",
                ephemeral=True,
            )


class ChannelSelectView(ui.View):
    def __init__(
        self,
        user: discord.User,
        channels: Sequence[discord.VoiceChannel],
        day: str,
        start: str,
        end: str,
    ):
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
            placeholder="�Q���҂�I���i�C�Ӂj",
            min_values=0,
            max_values=10,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent_view.owner.id:
            await interaction.response.send_message(
                "�\��҂̂ݎQ���҂�o�^�ł��܂�", ephemeral=True
            )
            return
        participants = [
            {"id": str(member.id), "name": member.mention} for member in self.values
        ]
        await sheets_call(
            sheets.update_participants, self.parent_view.row_index, participants
        )
        names = ", ".join(member.mention for member in self.values) if self.values else "�Ȃ�"
        await self.parent_view._send_announce(participants_text=names)
        await interaction.response.edit_message(view=None)


class ParticipantSelectView(ui.View):
    def __init__(
        self,
        row_index: int,
        owner: discord.User,
        channel_name: str,
        day: str,
        start: str,
        end: str,
        announce_channel: Optional[discord.TextChannel],
        user_mention: str,
    ):
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

    @ui.button(label="�X�L�b�v", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, _: ui.Button):
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message(
                "�\��҂̂ݑ���ł��܂�", ephemeral=True
            )
            return
        await self._send_announce(participants_text="�Ȃ�")
        await interaction.response.edit_message(content="�\�񂪊������܂���", view=None)

    async def _send_announce(self, participants_text: str):
        if not self.announce_channel:
            return
        if self.announce_channel.id != MODE_CONFIG.reservation_announce_channel_id:
            print(
                "?? Announce channel mismatch. "
                f"expected={MODE_CONFIG.reservation_announce_channel_id}, "
                f"actual={self.announce_channel.id}"
            )
            return
        embed = discord.Embed(
            title="? �\�񂪍쐬����܂���",
            description=f"{self.user_mention} �� {self.channel_name} ��\�񂵂܂���",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="���t", value=self.day, inline=True)
        embed.add_field(name="����", value=f"{self.start}?{self.end}", inline=True)
        embed.add_field(name="�Q����", value=participants_text or "�Ȃ�", inline=False)
        try:
            await self.announce_channel.send(embed=embed)
        except discord.HTTPException:
            pass


class CancelButtonView(ui.View):
    def __init__(self, row_index: int):
        super().__init__(timeout=120)
        self.row_index = row_index

    @ui.button(label="�L�����Z������", style=discord.ButtonStyle.danger)
    async def do_cancel(self, interaction: discord.Interaction, _: ui.Button):
        await sheets_call(sheets.delete_row, self.row_index)
        await interaction.response.edit_message(
            content="�\����L�����Z�����܂���", view=None
        )


class ReservationMenu(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(
        label="?? �\�񂷂�",
        style=discord.ButtonStyle.primary,
        custom_id="cafebook2:reserve",
    )
    async def reserve_btn(self, interaction: discord.Interaction, _: ui.Button):
        if interaction.response.is_done():
            return
        try:
            await interaction.response.send_modal(TimeInputModal(interaction.user))
        except discord.NotFound:
            return
        except discord.HTTPException as e:
            if e.code != 40060:
                raise

    @ui.button(
        label="? �L�����Z��",
        style=discord.ButtonStyle.danger,
        custom_id="cafebook2:cancel",
    )
    async def cancel_btn(self, interaction: discord.Interaction, _: ui.Button):
        if interaction.response.is_done():
            return
        await send_cancellation_embeds(interaction)

# --- �R�}���h & �C�x���g ---
async def send_cancellation_embeds(interaction: discord.Interaction):
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    my_reservations = await sheets_call(sheets.find_by_user, interaction.user.id)
    matches = [
        res
        for res in my_reservations
        if not is_past_reservation(res["day"], res["end"])
    ]

    if not matches:
        await interaction.followup.send(
            "���Ȃ��̗\�񂪌�����܂���ł���", ephemeral=True
        )
        return

    for res in matches:
        embed = discord.Embed(title="�\����e", color=discord.Color.orange())
        embed.add_field(name="�`�����l��", value=res["channel"], inline=True)
        embed.add_field(name="���t", value=res["day"], inline=True)
        embed.add_field(
            name="����", value=f"{res['start']}?{res['end']}", inline=True
        )
        participants = res.get("participants") or "[]"
        try:
            parsed_mentions = parse_participant_mentions(participants)
            mention_text = ", ".join(parsed_mentions) if parsed_mentions else "�Ȃ�"
        except json.JSONDecodeError:
            mention_text = "�Ȃ�"
        embed.add_field(name="�Q����", value=mention_text, inline=False)
        view = CancelButtonView(res["row_index"])
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@_maybe_guild_scope
@bot.tree.command(name="reserve_form", description="Show reserve form")
async def reserve_form(interaction: discord.Interaction):
    await interaction.response.send_modal(TimeInputModal(interaction.user))


@_maybe_guild_scope
@bot.tree.command(name="reserve_cancel", description="Cancel my reservation")
async def reserve_cancel(interaction: discord.Interaction):
    await send_cancellation_embeds(interaction)


@_maybe_guild_scope
@bot.tree.command(name="ping", description="Return Pong")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!", ephemeral=True)


@_maybe_guild_scope
@bot.tree.command(name="reserve", description="Log a reservation")
@app_commands.describe(item="Item", time="Time (HH:MM)")
async def reserve(interaction: discord.Interaction, item: str, time: str):
    await interaction.response.defer(ephemeral=True)
    try:
        parse_time(time)
    except ValueError:
        await interaction.followup.send("Time must be HH:MM", ephemeral=True)
        return
    await sheets_call(
        reserve_sheet.append_row,
        interaction.user.mention,
        item,
        time,
        interaction.user.id,
    )
    await interaction.followup.send(f"Logged: {item} {time}", ephemeral=True)


@_maybe_guild_scope
@bot.tree.command(name="show_menu", description="Show menu")
async def show_menu(interaction: discord.Interaction):
    view = ReservationMenu()
    await interaction.response.send_message("Choose an action", view=view)


@_maybe_guild_scope
@bot.tree.command(name="cafebook_panel", description="(�݊�) ���R�}���h �\�񃁃j���[��\��")
async def cafebook_panel(interaction: discord.Interaction):
    view = ReservationMenu()
    try:
        await interaction.response.send_message("�����I��ł�������", view=view)
    except discord.NotFound:
        return


async def _fetch_channel(channel_id: int) -> Optional[discord.abc.GuildChannel]:
    if channel_id <= 0:
        return None
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


@_maybe_guild_scope
@bot.tree.command(name="cafebook_status", description="���݂̃��[�h�Ɛݒ��\��")
async def cafebook_status(interaction: discord.Interaction):
    cfg = MODE_CONFIG
    guild = bot.get_guild(cfg.guild_id) if cfg.guild_id else None
    category = await resolve_cafe_category(bot, guild, cfg) if guild else None
    announce_channel = await _fetch_channel(cfg.reservation_announce_channel_id)
    reminder_channel = await _fetch_channel(cfg.reminder_channel_id)

    lines = [
        f"RUN_MODE: {RUN_MODE}",
        f"Guild ID: {cfg.guild_id or '���ݒ�'} / Guild Name: {guild.name if guild else '�s��'}",
        "Category ID: "
        f"{cfg.cafe_category_id or '���ݒ�'} / "
        f"Category Name: {cfg.cafe_category_name or '���ݒ�'} / "
        f"Resolved: {category.name if category else '�s��'}",
        "Announce Channel ID: "
        f"{cfg.reservation_announce_channel_id or '���ݒ�'} / "
        f"Name: {announce_channel.name if announce_channel else '�s��'}",
        "Reminder Channel ID: "
        f"{cfg.reminder_channel_id or '���ݒ�'} / "
        f"Name: {reminder_channel.name if reminder_channel else '�s��'}",
        f"Reminder Minutes: {cfg.reminder_minutes_before}",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.content.strip() == "�J�t�F�\��":
        view = ReservationMenu()
        await message.channel.send("�����I��ł�������", view=view)
        return
    await bot.process_commands(message)

# --- ���}�C���_�[ ---

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
    cfg = MODE_CONFIG
    if cfg.reminder_minutes_before <= 0 or cfg.reminder_channel_id <= 0:
        return
    channel = await _fetch_channel(cfg.reminder_channel_id)
    if channel is None:
        return
    if channel.id != cfg.reminder_channel_id:
        print(
            "?? Reminder channel mismatch. "
            f"expected={cfg.reminder_channel_id}, actual={channel.id}"
        )
        return

    now = datetime.now(JST)
    today_key = now.strftime("%Y/%m/%d")
    rows = await sheets_call(sheets.fetch_rows)
    for row_index, row in rows:
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
            start_dt = datetime.strptime(f"{day} {start}", "%Y/%m/%d %H:%M").replace(
                tzinfo=JST
            )
        except ValueError:
            continue
        delta = start_dt - now
        if timedelta(0) <= delta <= timedelta(minutes=cfg.reminder_minutes_before):
            mention_ids: List[int] = []
            seen_ids = set()
            if IS_TEST_MODE and BOT_TEST_USER_ID:
                mention_ids = [BOT_TEST_USER_ID]
            else:
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
            prefix = _mode_prefix()
            if mention_text:
                message = (
                    f"{prefix}{mention_text}\n"
                    f"�J�n{cfg.reminder_minutes_before}���O�ł��I"
                    f" {day} {row[3]}?{row[4]} / {row[1]}"
                )
            else:
                message = (
                    f"{prefix}�J�n{cfg.reminder_minutes_before}���O�ł��I"
                    f" {day} {row[3]}?{row[4]} / {row[1]}"
                )
            try:
                await channel.send(
                    message,
                    allowed_mentions=discord.AllowedMentions(
                        users=[discord.Object(id=uid) for uid in mention_ids]
                    ),
                )
            except discord.HTTPException:
                continue
            await sheets_call(sheets.mark_reminded, row_index)


@reminder_loop.before_loop
async def before_reminder_loop():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    await sheets_call(ensure_token)
    try:
        cmds = [cmd.name for cmd in bot.tree.walk_commands()]
        print(f"?? Loaded commands before sync: {cmds}")
        if IS_TEST_MODE and GUILD_OBJ:
            guild = bot.get_guild(MODE_CONFIG.guild_id)
            if guild is None:
                print(
                    f"?? Bot is not in guild {MODE_CONFIG.guild_id}. "
                    "Invite it with the applications.commands scope."
                )
            synced = await bot.tree.sync(guild=GUILD_OBJ)
            print(f"?? Synced {len(synced)} commands to guild {MODE_CONFIG.guild_id}")
            fetched = await bot.tree.fetch_commands(guild=GUILD_OBJ)
            print(f"?? Remote guild commands: {[c.name for c in fetched]}")
            if len(fetched) == 0:
                print(
                    "?? Guild sync returned 0. Check TEST_GUILD_ID and that the bot was invited with applications.commands."
                )
        else:
            synced = await bot.tree.sync()
            print(f"?? Globally synced {len(synced)} commands")
            fetched = await bot.tree.fetch_commands()
            print(f"?? Remote global commands: {[c.name for c in fetched]}")
        bot.add_view(ReservationMenu())
        if not reminder_loop.is_running():
            reminder_loop.start()
        print(
            "? bot ready as "
            f"{bot.user} (RUN_MODE={RUN_MODE}, GUILD_ID={MODE_CONFIG.guild_id})"
        )
        await _start_health_server()
    except Exception as exc:
        print(f"Failed to start bot: {exc}")


if __name__ == "__main__":
    ensure_token()
    bot.run(TOKEN)
