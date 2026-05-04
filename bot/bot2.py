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
from notion_client import Client as NotionClient
from supabase import Client, create_client

# --- 変数 ---
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
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "15425994cbf1467d9a8b330345643bcb")
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
            default="カフェ",
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


# --- Bot 設定 ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)
_health_app_started = False


# --- ユーティリティ ---

def _maybe_guild_scope(func):
    if IS_TEST_MODE and GUILD_OBJ:
        return app_commands.guilds(GUILD_OBJ)(func)
    return func


def _mode_prefix() -> str:
    return "[TEST] " if IS_TEST_MODE else ""


def _category_hint(guild: Optional[discord.Guild]) -> str:
    names = [cat.name for cat in guild.categories] if guild else []
    return (
        "カテゴリが見つかりません。\n"
        f"設定ID: {MODE_CONFIG.cafe_category_id or '未設定'} / "
        f"設定NAME: {MODE_CONFIG.cafe_category_name or '未設定'}\n"
        f"ギルドのカテゴリ一覧: {', '.join(names) if names else '取得できませんでした'}"
    )


def ensure_token() -> None:
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN を設定してください")
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL と SUPABASE_KEY を設定してください")
    if MODE_CONFIG.cafe_category_id <= 0 and not MODE_CONFIG.cafe_category_name:
        raise RuntimeError("CAFE_CATEGORY_ID/NAME を設定してください (PROD_/TEST_ のどちらか)")


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

# --- Supabase 操作 ---
class SupabaseOperations:
    def __init__(self) -> None:
        self.client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        self.notion_db_id = NOTION_DATABASE_ID
        self._notion: Optional[NotionClient] = (
            NotionClient(auth=NOTION_TOKEN) if NOTION_TOKEN and NOTION_DATABASE_ID else None
        )

    def _notion_create(
        self,
        row_id: int,
        user_mention: str,
        channel_name: str,
        day: str,
        start: str,
        end: str,
        user_id: int,
    ) -> Optional[str]:
        if not self._notion:
            return None
        try:
            page = self._notion.pages.create(
                parent={"database_id": self.notion_db_id},
                properties={
                    "予約者": {"title": [{"text": {"content": user_mention}}]},
                    "チャンネル": {"rich_text": [{"text": {"content": channel_name}}]},
                    "日付": {"rich_text": [{"text": {"content": day}}]},
                    "開始": {"rich_text": [{"text": {"content": start}}]},
                    "終了": {"rich_text": [{"text": {"content": end}}]},
                    "DiscordユーザーID": {"rich_text": [{"text": {"content": str(user_id)}}]},
                    "参加者": {"rich_text": [{"text": {"content": "[]"}}]},
                    "SupabaseID": {"number": row_id},
                    "リマインド済み": {"checkbox": False},
                },
            )
            return page["id"]
        except Exception as e:
            print(f"Notion create error: {e}")
            return None

    def _notion_update(self, row_id: int, properties: dict) -> None:
        if not self._notion:
            return
        try:
            resp = (
                self.client.table("reservations")
                .select("notion_page_id")
                .eq("id", row_id)
                .execute()
            )
            notion_page_id = resp.data[0].get("notion_page_id") if resp.data else None
            if notion_page_id:
                self._notion.pages.update(page_id=notion_page_id, properties=properties)
        except Exception as e:
            print(f"Notion update error: {e}")

    def _notion_archive(self, notion_page_id: str) -> None:
        if not self._notion or not notion_page_id:
            return
        try:
            self._notion.pages.update(page_id=notion_page_id, archived=True)
        except Exception as e:
            print(f"Notion archive error: {e}")

    def fetch_rows(self) -> List[Tuple[int, List[str]]]:
        response = self.client.table("reservations").select("*").order("id").execute()
        data: List[Tuple[int, List[str]]] = []
        for record in response.data:
            participants_raw = record.get("participants") or []
            row = [
                record.get("user_mention", ""),
                record.get("channel_name", ""),
                record.get("day", ""),
                record.get("start_time", ""),
                record.get("end_time", ""),
                str(record.get("user_id", "")),
                json.dumps(participants_raw, ensure_ascii=False),
                str(record.get("created_at", "")),
                "TRUE" if record.get("reminded") else "FALSE",
            ]
            data.append((record["id"], row))
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
        response = (
            self.client.table("reservations")
            .insert({
                "user_mention": user_mention,
                "channel_name": channel_name,
                "day": day,
                "start_time": start,
                "end_time": end,
                "user_id": user_id,
                "participants": [],
                "reminded": False,
            })
            .execute()
        )
        row_id = response.data[0]["id"]
        notion_page_id = self._notion_create(
            row_id, user_mention, channel_name, day, start, end, user_id
        )
        if notion_page_id:
            self.client.table("reservations").update(
                {"notion_page_id": notion_page_id}
            ).eq("id", row_id).execute()
        return row_id

    def update_participants(
        self, row_id: int, participants: Sequence[Dict[str, str]]
    ) -> None:
        self.client.table("reservations").update(
            {"participants": list(participants)}
        ).eq("id", row_id).execute()
        self._notion_update(
            row_id,
            {"参加者": {"rich_text": [{"text": {"content": json.dumps(list(participants), ensure_ascii=False)}}]}},
        )

    def mark_reminded(self, row_id: int) -> None:
        self.client.table("reservations").update(
            {"reminded": True}
        ).eq("id", row_id).execute()
        self._notion_update(row_id, {"リマインド済み": {"checkbox": True}})

    def delete_row(self, row_id: int) -> None:
        resp = (
            self.client.table("reservations")
            .select("notion_page_id")
            .eq("id", row_id)
            .execute()
        )
        notion_page_id = resp.data[0].get("notion_page_id") if resp.data else None
        self.client.table("reservations").delete().eq("id", row_id).execute()
        if notion_page_id:
            self._notion_archive(notion_page_id)

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
        response = (
            self.client.table("reservations")
            .select("*")
            .eq("user_id", user_id)
            .execute()
        )
        results: List[Dict[str, str]] = []
        for record in response.data:
            participants_raw = record.get("participants") or []
            results.append({
                "row_index": record["id"],
                "user": record.get("user_mention", ""),
                "channel": record.get("channel_name", ""),
                "day": record.get("day", ""),
                "start": record.get("start_time", ""),
                "end": record.get("end_time", ""),
                "participants": json.dumps(participants_raw, ensure_ascii=False),
                "created_at": str(record.get("created_at", "")),
            })
        return results


sheets = SupabaseOperations()


async def sheets_call(func, *args, **kwargs):
    """Supabaseへのブロッキング呼び出しを別スレッドで実行する"""
    return await asyncio.to_thread(func, *args, **kwargs)

# --- Simple reserve logging ---
class SupabaseReserveLog:
    def __init__(self) -> None:
        self.client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

    def append_row(self, user_mention: str, item: str, time_text: str, user_id: int) -> None:
        self.client.table("reserve_logs").insert({
            "user_mention": user_mention,
            "item": item,
            "time_text": time_text,
            "user_id": user_id,
        }).execute()


reserve_sheet = SupabaseReserveLog()


# --- UI コンポーネント ---
class TimeInputModal(ui.Modal, title="🕐 予約時間を入力"):
    def __init__(self, user: discord.User):
        super().__init__(timeout=300)
        self.request_user = user
        self.day = ui.TextInput(
            label="日付(YYYY/MM/DD)",
            default=datetime.now(JST).strftime("%Y/%m/%d"),
        )
        self.start_time = ui.TextInput(label="開始(HH:MM)", default="13:00")
        self.end_time = ui.TextInput(label="終了(HH:MM)", default="14:00")
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
                "日付または時間の形式が正しくありません", ephemeral=True
            )
            return

        if start_t >= end_t:
            await interaction.followup.send(
                "開始時間は終了時間より前にしてください", ephemeral=True
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
                "指定時間に空いているチャンネルがありません", ephemeral=True
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
            f"{self.day.value} {self.start_time.value}〜{self.end_time.value} で予約するチャンネルを選んでください",
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
            placeholder="チャンネルを選択",
            min_values=1,
            max_values=1,
            options=options[:25],
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent_view.user.id:
            await interaction.response.send_message(
                "予約者のみ選択できます", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        channel_id = int(self.values[0])
        channel = discord.utils.get(self.parent_view.channels, id=channel_id)
        if not channel:
            await interaction.followup.send("チャンネルが見つかりません", ephemeral=True)
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
                "予約を登録しました。\n"
                f"チャンネル: {channel.name}\n"
                f"日付: {self.parent_view.day}\n"
                f"時間: {self.parent_view.start}〜{self.parent_view.end}\n"
                "参加者を追加しますか？（任意・スキップ）"
            ),
            view=participant_view,
            ephemeral=True,
        )
        if (
            MODE_CONFIG.reservation_announce_channel_id
            and participant_view.announce_channel is None
        ):
            await interaction.followup.send(
                "予約アナウンスチャンネルが見つかりませんでした",
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
            placeholder="参加者を選択（任意）",
            min_values=0,
            max_values=10,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent_view.owner.id:
            await interaction.response.send_message(
                "予約者のみ参加者を登録できます", ephemeral=True
            )
            return
        participants = [
            {"id": str(member.id), "name": member.mention} for member in self.values
        ]
        await sheets_call(
            sheets.update_participants, self.parent_view.row_index, participants
        )
        names = ", ".join(member.mention for member in self.values) if self.values else "なし"
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

    @ui.button(label="スキップ", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, _: ui.Button):
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message(
                "予約者のみ選択できます", ephemeral=True
            )
            return
        await self._send_announce(participants_text="なし")
        await interaction.response.edit_message(content="予約が完了しました", view=None)

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
            title="✅ 予約が作成されました",
            description=f"{self.user_mention} が {self.channel_name} を予約しました",
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
        await sheets_call(sheets.delete_row, self.row_index)
        await interaction.response.edit_message(
            content="予約をキャンセルしました", view=None
        )


class ReservationMenu(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(
        label="📅 予約する",
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
        label="❌ キャンセル",
        style=discord.ButtonStyle.danger,
        custom_id="cafebook2:cancel",
    )
    async def cancel_btn(self, interaction: discord.Interaction, _: ui.Button):
        if interaction.response.is_done():
            return
        await send_cancellation_embeds(interaction)

# --- コマンド & イベント ---
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
            "あなたの予約が見つかりませんでした", ephemeral=True
        )
        return

    for res in matches:
        embed = discord.Embed(title="予約内容", color=discord.Color.orange())
        embed.add_field(name="チャンネル名", value=res["channel"], inline=True)
        embed.add_field(name="日付", value=res["day"], inline=True)
        embed.add_field(
            name="時間", value=f"{res['start']}〜{res['end']}", inline=True
        )
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
@bot.tree.command(name="cafebook_panel", description="(互換) 旧コマンド 予約メニューを表示")
async def cafebook_panel(interaction: discord.Interaction):
    view = ReservationMenu()
    try:
        await interaction.response.send_message("アクションを選択してください", view=view)
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
@bot.tree.command(name="cafebook_status", description="現在のモードと設定を表示")
async def cafebook_status(interaction: discord.Interaction):
    cfg = MODE_CONFIG
    guild = bot.get_guild(cfg.guild_id) if cfg.guild_id else None
    category = await resolve_cafe_category(bot, guild, cfg) if guild else None
    announce_channel = await _fetch_channel(cfg.reservation_announce_channel_id)
    reminder_channel = await _fetch_channel(cfg.reminder_channel_id)

    lines = [
        f"RUN_MODE: {RUN_MODE}",
        f"Guild ID: {cfg.guild_id or '未設定'} / Guild Name: {guild.name if guild else '不明'}",
        "Category ID: "
        f"{cfg.cafe_category_id or '未設定'} / "
        f"Category Name: {cfg.cafe_category_name or '未設定'} / "
        f"Resolved: {category.name if category else '不明'}",
        "Announce Channel ID: "
        f"{cfg.reservation_announce_channel_id or '未設定'} / "
        f"Name: {announce_channel.name if announce_channel else '不明'}",
        "Reminder Channel ID: "
        f"{cfg.reminder_channel_id or '未設定'} / "
        f"Name: {reminder_channel.name if reminder_channel else '不明'}",
        f"Reminder Minutes: {cfg.reminder_minutes_before}",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.content.strip() == "カフェ予約":
        view = ReservationMenu()
        await message.channel.send("アクションを選択してください", view=view)
        return
    await bot.process_commands(message)

# --- リマインダー ---

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
                    f"開始{cfg.reminder_minutes_before}分前です！"
                    f" {day} {row[3]}?{row[4]} / {row[1]}"
                )
            else:
                message = (
                    f"{prefix}開始{cfg.reminder_minutes_before}分前です！"
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
    except Exception as exc:
        print(f"Failed to start bot: {exc}")

async def main():
    await _start_health_server()
    ensure_token()
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())