import discord
from discord.ext import commands, tasks
import sqlite3
import asyncio
import datetime
import re
from typing import List, Optional, Dict, Any
import json
import logging
import os

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8')  # ログファイルに保存
    ]
)
logger = logging.getLogger(__name__)

# Bot設定
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# 24時間稼働のための最適化設定
bot = commands.Bot(
    command_prefix='!', 
    intents=intents, 
    help_command=None,
    max_messages=100,   # メッセージキャッシュを大幅制限
    chunk_guilds_at_startup=False,  # 起動時のギルドチャンクを無効化
    enable_debug_events=False,  # デバッグイベントを無効化
    activity=discord.Activity(type=discord.ActivityType.watching, name="タスク管理"),  # アクティビティ表示
    heartbeat_timeout=60.0,  # ハートビートタイムアウトを延長
    max_ratelimit_timeout=300.0  # レート制限タイムアウトを延長
)

# 重複実行防止用のセット
executing_commands = set()

# リマインダー送信済みタスクを記録するセット（メモリ内）
reminded_tasks = set()

# メモリ管理用の定期的なクリーンアップ
@tasks.loop(hours=1)
async def cleanup_memory():
    """メモリ使用量を最適化"""
    try:
        # 古いメッセージキャッシュをクリア
        if hasattr(bot, '_connection') and hasattr(bot._connection, '_messages'):
            bot._connection._messages.clear()
        
        # リマインダータスクセットをクリア
        reminded_tasks.clear()
        
        logger.info("Memory cleanup completed")
    except Exception as e:
        logger.error(f"Memory cleanup error: {e}")

# データベース初期化
def init_database():
    conn = sqlite3.connect('reminder_bot.db')
    cursor = conn.cursor()
    
    # 管理者テーブル
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            guild_id INTEGER,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 指示権限者テーブル
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS instructors (
            user_id INTEGER,
            guild_id INTEGER,
            target_users TEXT,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, guild_id)
        )
    ''')
    
    # タスクテーブル（リマインダー送信フラグを追加）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            instructor_id INTEGER,
            assignee_id INTEGER,
            task_name TEXT,
            due_date TIMESTAMP,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            message_id INTEGER,
            channel_id INTEGER,
            reminder_sent INTEGER DEFAULT 0
        )
    ''')
    
    # 通知チャンネルテーブル
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS notification_channels (
            guild_id INTEGER,
            user_id INTEGER,
            channel_id INTEGER,
            channel_type TEXT,
            PRIMARY KEY (guild_id, user_id, channel_type)
        )
    ''')
    
    # 既存のテーブルにreminder_sentカラムを追加（存在しない場合）
    try:
        cursor.execute('ALTER TABLE tasks ADD COLUMN reminder_sent INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass  # カラムが既に存在する場合
    
    conn.commit()
    conn.close()

# スレッド削除機能
async def delete_thread_after_delay(thread, delay_seconds):
    """指定時間後にスレッドを削除"""
    try:
        await asyncio.sleep(delay_seconds)
        
        # スレッドがアーカイブされている場合は先に復元
        if thread.archived:
            await thread.edit(archived=False)
            await asyncio.sleep(1)  # 少し待機
        
        await thread.delete()
        logger.info(f"Thread {thread.name} deleted after {delay_seconds} seconds")
    except discord.NotFound:
        logger.info(f"Thread {thread.name} already deleted")
    except Exception as e:
        logger.error(f"Error deleting thread {thread.name}: {e}")

# データベース操作関数
class DatabaseManager:
    @staticmethod
    def execute_query(query: str, params: tuple = None):
        conn = sqlite3.connect('reminder_bot.db')
        cursor = conn.cursor()
        if params:
            cursor.execute(query, params)
        else:
            cursor.execute(query)
        result = cursor.fetchall()
        conn.commit()
        conn.close()
        return result
    
    @staticmethod
    def is_admin(user_id: int, guild_id: int) -> bool:
        result = DatabaseManager.execute_query(
            "SELECT 1 FROM admins WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id)
        )
        return len(result) > 0
    
    @staticmethod
    def is_instructor(user_id: int, guild_id: int) -> bool:
        result = DatabaseManager.execute_query(
            "SELECT 1 FROM instructors WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id)
        )
        return len(result) > 0
    
    @staticmethod
    def can_instruct_user(instructor_id: int, target_id: int, guild_id: int) -> bool:
        if DatabaseManager.is_admin(instructor_id, guild_id):
            return True
        
        result = DatabaseManager.execute_query(
            "SELECT target_users FROM instructors WHERE user_id = ? AND guild_id = ?",
            (instructor_id, guild_id)
        )
        
        if not result:
            return False
        
        target_users = json.loads(result[0][0]) if result[0][0] else []
        return target_id in target_users or not target_users  # 空リストは全員対象
    
    @staticmethod
    def add_task(guild_id: int, instructor_id: int, assignee_id: int, 
                task_name: str, due_date: datetime.datetime, message_id: int, channel_id: int):
        DatabaseManager.execute_query(
            "INSERT INTO tasks (guild_id, instructor_id, assignee_id, task_name, due_date, message_id, channel_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, instructor_id, assignee_id, task_name, due_date, message_id, channel_id)
        )
    
    @staticmethod
    def update_task_status(task_id: int, status: str):
        DatabaseManager.execute_query(
            "UPDATE tasks SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, task_id)
        )
    
    @staticmethod
    def check_duplicate_task(assignee_id: int, task_name: str, guild_id: int) -> bool:
        result = DatabaseManager.execute_query(
            "SELECT 1 FROM tasks WHERE assignee_id = ? AND task_name = ? AND guild_id = ? AND status NOT IN ('completed', 'abandoned', 'declined')",
            (assignee_id, task_name, guild_id)
        )
        return len(result) > 0

    @staticmethod
    def add_instructor_if_not_exists(user_id: int, guild_id: int, target_users: list) -> bool:
        """指示者が存在しない場合のみ追加し、追加されたかどうかを返す"""
        # 既存チェック
        existing = DatabaseManager.execute_query(
            "SELECT 1 FROM instructors WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id)
        )
        
        if existing:
            return False  # 既に存在する
        
        # 新規追加
        DatabaseManager.execute_query(
            "INSERT INTO instructors (user_id, guild_id, target_users) VALUES (?, ?, ?)",
            (user_id, guild_id, json.dumps(target_users))
        )
        return True  # 新規追加された

    @staticmethod
    def add_admin_if_not_exists(user_id: int, guild_id: int) -> bool:
        """管理者が存在しない場合のみ追加し、追加されたかどうかを返す"""
        # 既存チェック
        existing = DatabaseManager.execute_query(
            "SELECT 1 FROM admins WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id)
        )
        
        if existing:
            return False  # 既に存在する
        
        # 新規追加
        DatabaseManager.execute_query(
            "INSERT INTO admins (user_id, guild_id) VALUES (?, ?)",
            (user_id, guild_id)
        )
        return True  # 新規追加された

    @staticmethod
    def mark_reminder_sent(task_id: int):
        """リマインダー送信フラグを設定"""
        DatabaseManager.execute_query(
            "UPDATE tasks SET reminder_sent = 1 WHERE id = ?",
            (task_id,)
        )

# 日付解析関数（時間指定対応版）
def parse_date(date_str: str) -> Optional[datetime.datetime]:
    now = datetime.datetime.now()
    
    # 時間部分を分離
    time_part = None
    date_part = date_str.strip()
    
    # 時間指定がある場合（HH:MM形式）
    time_match = re.search(r'(\d{1,2}):(\d{2})$', date_str)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        
        # 時間の妥当性チェック
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            time_part = (hour, minute)
            date_part = date_str[:time_match.start()].strip()
        else:
            return None
    
    # デフォルト時間（時間指定がない場合）
    default_hour = 23
    default_minute = 59
    
    # 相対指定（大幅に拡張）
    if date_part == "今日" or date_part == "きょう":
        base_date = now
    elif date_part == "明日" or date_part == "あした" or date_part == "あす":
        base_date = now + datetime.timedelta(days=1)
    elif date_part == "明後日" or date_part == "あさって":
        base_date = now + datetime.timedelta(days=2)
    elif date_part == "昨日" or date_part == "きのう":
        base_date = now - datetime.timedelta(days=1)
    elif date_part == "一昨日" or date_part == "おととい":
        base_date = now - datetime.timedelta(days=2)
    elif "時間後" in date_part or "じかんご" in date_part:
        try:
            hours = int(date_part.replace("時間後", "").replace("じかんご", ""))
            base_date = now + datetime.timedelta(hours=hours)
        except ValueError:
            return None
    elif "分後" in date_part or "ふんご" in date_part:
        try:
            minutes = int(date_part.replace("分後", "").replace("ふんご", ""))
            base_date = now + datetime.timedelta(minutes=minutes)
        except ValueError:
            return None
    elif "日後" in date_part:
        try:
            days = int(date_part.replace("日後", ""))
            base_date = now + datetime.timedelta(days=days)
        except ValueError:
            return None
    elif "週間後" in date_part or "しゅうかんご" in date_part:
        try:
            weeks = int(date_part.replace("週間後", "").replace("しゅうかんご", ""))
            base_date = now + datetime.timedelta(weeks=weeks)
        except ValueError:
            return None
    elif "ヶ月後" in date_part or "かげつご" in date_part or "ヶ月後" in date_part:
        try:
            months = int(date_part.replace("ヶ月後", "").replace("かげつご", "").replace("ヶ月後", ""))
            # 簡易的な月計算（30日として計算）
            base_date = now + datetime.timedelta(days=months * 30)
        except ValueError:
            return None
    elif "来週" in date_part or "らいしゅう" in date_part:
        # 来週の月曜日
        days_ahead = 7 - now.weekday()  # 月曜日は0
        if days_ahead <= 0:  # 今日が月曜日以降の場合
            days_ahead += 7
        base_date = now + datetime.timedelta(days=days_ahead)
    elif "来月" in date_part or "らいげつ" in date_part:
        # 来月の1日
        if now.month == 12:
            base_date = now.replace(year=now.year + 1, month=1, day=1)
        else:
            base_date = now.replace(month=now.month + 1, day=1)
    elif "月末" in date_part or "げつまつ" in date_part:
        # 今月末
        if now.month == 12:
            base_date = now.replace(year=now.year + 1, month=1, day=1) - datetime.timedelta(days=1)
        else:
            base_date = now.replace(month=now.month + 1, day=1) - datetime.timedelta(days=1)
    elif "金曜日" in date_part or "金曜" in date_part or "きんようび" in date_part:
        # 今週または来週の金曜日
        days_ahead = 4 - now.weekday()  # 金曜日は4
        if days_ahead <= 0:  # 今日が金曜日以降の場合
            days_ahead += 7
        base_date = now + datetime.timedelta(days=days_ahead)
    elif "月曜日" in date_part or "月曜" in date_part or "げつようび" in date_part:
        days_ahead = 0 - now.weekday()  # 月曜日は0
        if days_ahead <= 0:  # 今日が月曜日以降の場合
            days_ahead += 7
        base_date = now + datetime.timedelta(days=days_ahead)
    elif "火曜日" in date_part or "火曜" in date_part or "かようび" in date_part:
        days_ahead = 1 - now.weekday()  # 火曜日は1
        if days_ahead <= 0:  # 今日が火曜日以降の場合
            days_ahead += 7
        base_date = now + datetime.timedelta(days=days_ahead)
    elif "水曜日" in date_part or "水曜" in date_part or "すいようび" in date_part:
        days_ahead = 2 - now.weekday()  # 水曜日は2
        if days_ahead <= 0:  # 今日が水曜日以降の場合
            days_ahead += 7
        base_date = now + datetime.timedelta(days=days_ahead)
    elif "木曜日" in date_part or "木曜" in date_part or "もくようび" in date_part:
        days_ahead = 3 - now.weekday()  # 木曜日は3
        if days_ahead <= 0:  # 今日が木曜日以降の場合
            days_ahead += 7
        base_date = now + datetime.timedelta(days=days_ahead)
    elif "土曜日" in date_part or "土曜" in date_part or "どようび" in date_part:
        days_ahead = 5 - now.weekday()  # 土曜日は5
        if days_ahead <= 0:  # 今日が土曜日以降の場合
            days_ahead += 7
        base_date = now + datetime.timedelta(days=days_ahead)
    elif "日曜日" in date_part or "日曜" in date_part or "にちようび" in date_part:
        days_ahead = 6 - now.weekday()  # 日曜日は6
        if days_ahead <= 0:  # 今日が日曜日以降の場合
            days_ahead += 7
        base_date = now + datetime.timedelta(days=days_ahead)
    else:
        # 絶対指定（拡張版）
        base_date = None
        date_patterns = [
            (r'(\d{1,2})/(\d{1,2})', "%m/%d"),
            (r'(\d{4})/(\d{1,2})/(\d{1,2})', "%Y/%m/%d"),
            (r'(\d{1,2})-(\d{1,2})', "%m-%d"),
            (r'(\d{4})-(\d{1,2})-(\d{1,2})', "%Y-%m-%d"),
            (r'(\d{1,2})月(\d{1,2})日', "%m月%d日"),
            (r'(\d{4})年(\d{1,2})月(\d{1,2})日', "%Y年%m月%d日"),
        ]
        
        for pattern, format_str in date_patterns:
            match = re.match(pattern, date_part)
            if match:
                try:
                    if len(match.groups()) == 2:
                        # MM/DD形式の場合
                        if "/" in pattern:
                            base_date = datetime.datetime.strptime(f"{now.year}/{date_part}", "%Y/%m/%d")
                        elif "-" in pattern:
                            base_date = datetime.datetime.strptime(f"{now.year}-{date_part}", "%Y-%m-%d")
                        else:
                            base_date = datetime.datetime.strptime(f"{now.year}年{date_part}", "%Y年%m月%d日")
                        
                        if base_date < now.replace(hour=0, minute=0, second=0, microsecond=0):
                            base_date = base_date.replace(year=now.year + 1)
                    else:
                        base_date = datetime.datetime.strptime(date_part, format_str)
                    break
                except ValueError:
                    continue
        
        if base_date is None:
            return None
    
    # 時間を設定
    if time_part:
        hour, minute = time_part
        result_date = base_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
    else:
        # 時間指定がない場合は23:59
        result_date = base_date.replace(hour=default_hour, minute=default_minute, second=0, microsecond=0)
    
    return result_date
    
# タスクビュークラス（修正版）
class TaskView(discord.ui.View):
    def __init__(self, task_id: int, assignee_id: int, instructor_id: int, status: str = "pending"):
        super().__init__(timeout=None)
        self.task_id = task_id
        self.assignee_id = assignee_id
        self.instructor_id = instructor_id
        
        # 状態に応じてボタンを設定
        self.setup_buttons(status)
    
    def setup_buttons(self, status: str):
        """状態に応じてボタンを設定"""
        self.clear_items()
        
        if status == "pending":
            # 未受託状態：承諾か相談
            self.add_item(discord.ui.Button(
                label="✅ 受託する", 
                style=discord.ButtonStyle.success, 
                custom_id=f"accept_task_{self.task_id}"
            ))
            self.add_item(discord.ui.Button(
                label="❌ 相談します", 
                style=discord.ButtonStyle.secondary, 
                custom_id=f"decline_task_{self.task_id}"
            ))
        elif status == "accepted":
            # 受託済み状態：完了報告か問題発生
            self.add_item(discord.ui.Button(
                label="📝 完了報告", 
                style=discord.ButtonStyle.primary, 
                custom_id=f"complete_task_{self.task_id}"
            ))
            self.add_item(discord.ui.Button(
                label="⚠️ 問題発生", 
                style=discord.ButtonStyle.danger, 
                custom_id=f"abandon_task_{self.task_id}"
            ))
        elif status == "completed":
            # 完了状態：取り消しボタンのみ
            self.add_item(discord.ui.Button(
                label="↩️ 完了を取り消す", 
                style=discord.ButtonStyle.secondary, 
                custom_id=f"undo_completion_{self.task_id}"
            ))
    
    # 元のボタンメソッドは削除し、interaction処理は別途実装
    
    async def send_notification(self, guild, message, user, assignee_id=None):
        """指示者に完了通知を送信"""
        try:
            # 完了者のIDを取得
            if assignee_id is None:
                assignee_id = self.assignee_id
            
            # 1. タスク管理チャンネルに通知
            task_channel = discord.utils.get(guild.channels, name="タスク管理")
            if task_channel and isinstance(task_channel, discord.TextChannel):
                embed = discord.Embed(
                    title="🎉 タスク完了通知",
                    description=f"{user.mention} {message}",
                    color=discord.Color.green(),
                    timestamp=datetime.datetime.now()
                )
                embed.add_field(name="完了者", value=f"<@{assignee_id}>", inline=True)
                embed.add_field(name="タスクID", value=f"#{self.task_id}", inline=True)
                await task_channel.send(embed=embed)
                logger.info(f"タスク管理チャンネルに完了通知を送信: {task_channel.name}")
            
            # 2. 指示者の個人チャンネルにも通知
            personal_channel_name = f"{user.display_name}のタスク"
            personal_channel = discord.utils.get(guild.channels, name=personal_channel_name)
            if personal_channel and isinstance(personal_channel, discord.TextChannel):
                embed = discord.Embed(
                    title="🎉 タスク完了",
                    description=f"あなたが指示したタスクが完了されました！",
                    color=discord.Color.green(),
                    timestamp=datetime.datetime.now()
                )
                embed.add_field(name="タスク", value=message, inline=False)
                embed.add_field(name="完了者", value=f"<@{assignee_id}>", inline=True)
                embed.add_field(name="タスクID", value=f"#{self.task_id}", inline=True)
                await personal_channel.send(embed=embed)
                logger.info(f"個人チャンネルに完了通知を送信: {personal_channel.name}")
            
            # 3. 指示者にDM通知（オプション）
            try:
                embed = discord.Embed(
                    title="🎉 タスク完了通知",
                    description=f"あなたが指示したタスクが完了されました！",
                    color=discord.Color.green(),
                    timestamp=datetime.datetime.now()
                )
                embed.add_field(name="タスク", value=message, inline=False)
                embed.add_field(name="完了者", value=f"<@{assignee_id}>", inline=True)
                embed.add_field(name="タスクID", value=f"#{self.task_id}", inline=True)
                await user.send(embed=embed)
                logger.info(f"指示者にDMで完了通知を送信: {user.display_name}")
            except discord.Forbidden:
                logger.info(f"指示者 {user.display_name} のDMが無効です")
            except Exception as dm_error:
                logger.error(f"DM送信エラー: {dm_error}")
                
        except Exception as e:
            logger.error(f"通知送信中にエラーが発生: {e}")
            raise

# 完了取り消しビューは削除（TaskViewに統合）

# 古いsetup_persistent_views関数は削除済み

# Persistent View の登録（修正版）
async def setup_persistent_views():
    """永続化ビューの設定"""
    
    @bot.event
    async def on_interaction(interaction):
        if interaction.type != discord.InteractionType.component:
            return
        
        custom_id = interaction.data.get('custom_id')
        if not custom_id:
            return
        
        # カスタムIDからアクションとタスクIDを分離
        parts = custom_id.split('_')
        if len(parts) < 3:
            return
        
        action = '_'.join(parts[:-1])  # "accept_task", "complete_task" など
        try:
            task_id = int(parts[-1])
        except ValueError:
            return
        
        if action not in ['accept_task', 'decline_task', 'complete_task', 'abandon_task', 'undo_completion']:
            return
        
        try:
            # データベースからタスク情報を取得
            task_data = DatabaseManager.execute_query(
                "SELECT assignee_id, instructor_id, status FROM tasks WHERE id = ?",
                (task_id,)
            )
            
            if not task_data:
                await interaction.response.send_message("❌ タスクが見つかりません。", ephemeral=True)
                return
            
            assignee_id, instructor_id, current_status = task_data[0]
            
            # 権限チェック
            if interaction.user.id != assignee_id:
                await interaction.response.send_message("❌ このタスクの担当者ではありません。", ephemeral=True)
                return
            
            # アクション実行
            await handle_task_action(interaction, action, task_id, assignee_id, instructor_id, current_status)
            
        except Exception as e:
            logger.error(f"Error handling persistent interaction: {e}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ エラーが発生しました。", ephemeral=True)
                else:
                    await interaction.followup.send("❌ エラーが発生しました。", ephemeral=True)
            except:
                pass

async def handle_task_action(interaction, action, task_id, assignee_id, instructor_id, current_status):
    """タスクアクションの処理"""
    embed = interaction.message.embeds[0] if interaction.message.embeds else None
    if not embed:
        await interaction.response.send_message("❌ メッセージ情報が取得できませんでした。", ephemeral=True)
        return
    
    try:
        if action == "accept_task":
            # 受託処理
            DatabaseManager.update_task_status(task_id, "accepted")
            
            # Embedを更新
            embed.color = discord.Color.blue()
            # 状態フィールドを更新（フィールドインデックスを確認）
            for i, field in enumerate(embed.fields):
                if field.name == "状態":
                    embed.set_field_at(i, name="状態", value="✅ 受託済み", inline=True)
                    break
            
            # ボタンを更新
            view = TaskView(task_id, assignee_id, instructor_id, "accepted")
            
            await interaction.response.edit_message(embed=embed, view=view)
            
            # 通知送信
            guild = interaction.guild
            if guild:
                instructor = guild.get_member(instructor_id)
                if instructor:
                    await send_notification_to_instructor(guild, instructor, f"✅ 受託", assignee_id, task_id)
        
        elif action == "decline_task":
            # 辞退処理
            DatabaseManager.update_task_status(task_id, "declined")
            
            embed.color = discord.Color.red()
            for i, field in enumerate(embed.fields):
                if field.name == "状態":
                    embed.set_field_at(i, name="状態", value="❌ 辞退", inline=True)
                    break
            
            # ボタンを削除
            await interaction.response.edit_message(embed=embed, view=None)
            
            # 通知送信
            guild = interaction.guild
            if guild:
                instructor = guild.get_member(instructor_id)
                if instructor:
                    await send_notification_to_instructor(guild, instructor, f"❌ 辞退", assignee_id, task_id)
        
        elif action == "complete_task":
            # 完了処理
            DatabaseManager.update_task_status(task_id, "completed")
            
            embed.color = discord.Color.green()
            for i, field in enumerate(embed.fields):
                if field.name == "状態":
                    embed.set_field_at(i, name="状態", value="🎉 完了", inline=True)
                    break
            
            # 完了取り消しボタンを表示
            view = TaskView(task_id, assignee_id, instructor_id, "completed")
            
            await interaction.response.edit_message(embed=embed, view=view)
            
            # 完了通知送信
            guild = interaction.guild
            if guild:
                instructor = guild.get_member(instructor_id)
                if instructor:
                    await send_notification_to_instructor(guild, instructor, f"🎉 完了", assignee_id, task_id)
        
        elif action == "abandon_task":
            # 問題発生処理
            DatabaseManager.update_task_status(task_id, "abandoned")
            
            embed.color = discord.Color.dark_red()
            for i, field in enumerate(embed.fields):
                if field.name == "状態":
                    embed.set_field_at(i, name="状態", value="⚠️ 問題発生", inline=True)
                    break
            
            await interaction.response.edit_message(embed=embed, view=None)
            
            # 通知送信
            guild = interaction.guild
            if guild:
                instructor = guild.get_member(instructor_id)
                if instructor:
                    await send_notification_to_instructor(guild, instructor, f"⚠️ 問題発生", assignee_id, task_id)
        
        elif action == "undo_completion":
            # 完了取り消し処理
            DatabaseManager.update_task_status(task_id, "accepted")
            
            embed.color = discord.Color.blue()
            for i, field in enumerate(embed.fields):
                if field.name == "状態":
                    embed.set_field_at(i, name="状態", value="✅ 受託済み", inline=True)
                    break
            
            # 元のボタンに戻す
            view = TaskView(task_id, assignee_id, instructor_id, "accepted")
            
            await interaction.response.edit_message(embed=embed, view=view)
            await interaction.followup.send("完了を取り消しました。タスクは受託済み状態に戻りました。", ephemeral=True)
    
    except Exception as e:
        logger.error(f"Error in handle_task_action: {e}")
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ エラーが発生しました。", ephemeral=True)
        else:
            await interaction.followup.send("❌ エラーが発生しました。", ephemeral=True)

async def send_notification_to_instructor(guild, instructor, message, assignee_id, task_id):
    """指示者に通知を送信"""
    try:
        # 1. タスク管理チャンネルに通知（スレッド作成）
        task_channel = discord.utils.get(guild.channels, name="タスク管理")
        if task_channel and isinstance(task_channel, discord.TextChannel):
            # メインメッセージ（シンプル）
            main_embed = discord.Embed(
                title=message,
                color=discord.Color.blue()
            )
            main_message = await task_channel.send(embed=main_embed)
            
            # スレッドを作成
            thread_name = f"📋 タスク状況 - {message}"
            thread = await main_message.create_thread(
                name=thread_name, 
                auto_archive_duration=60,
                reason="タスク状況詳細"
            )
            
            # スレッドに詳細情報を送信（指示者、状態、作成日時のみ）
            detail_embed = discord.Embed(
                title="📋 詳細情報",
                color=discord.Color.blue()
            )
            detail_embed.add_field(name="指示者", value=instructor.mention, inline=True)
            detail_embed.add_field(name="状態", value=message, inline=True)
            detail_embed.add_field(name="更新日時", value=datetime.datetime.now().strftime("%Y/%m/%d %H:%M"), inline=True)
            
            await thread.send(embed=detail_embed)
            
            # スレッドに適切なユーザーを招待（レート制限対策）
            try:
                # 招待するユーザーのリストを作成（重複を避ける）
                users_to_add = set()
                users_to_add.add(instructor)
                
                # 管理者ロールを持つユーザーを追加
                admin_role = discord.utils.get(guild.roles, name="タスク管理者")
                if admin_role:
                    for member in admin_role.members:
                        users_to_add.add(member)
                
                # 指示者ロールを持つユーザーを追加
                instructor_role = discord.utils.get(guild.roles, name="タスク指示者")
                if instructor_role:
                    for member in instructor_role.members:
                        users_to_add.add(member)
                
                # ユーザーを順次招待（レート制限対策）
                for user in users_to_add:
                    try:
                        await thread.add_user(user)
                        await asyncio.sleep(0.5)  # 0.5秒の遅延
                    except Exception as user_error:
                        logger.error(f"Failed to add user {user.display_name} to thread: {user_error}")
                        continue
                        
            except Exception as e:
                logger.error(f"Failed to add users to thread: {e}")
            
            # 完了の場合、5分後にスレッドを削除するタスクをスケジュール
            if "完了" in message:
                asyncio.create_task(delete_thread_after_delay(thread, 300))  # 5分 = 300秒
        
        # 2. 指示者の個人チャンネルに通知（スレッド作成）
        personal_channel_name = f"{instructor.display_name}のタスク"
        personal_channel = discord.utils.get(guild.channels, name=personal_channel_name)
        if personal_channel and isinstance(personal_channel, discord.TextChannel):
            # メインメッセージ（シンプル）
            main_embed = discord.Embed(
                title=message,
                color=discord.Color.blue()
            )
            main_message = await personal_channel.send(embed=main_embed)
            
            # スレッドを作成
            thread_name = f"📋 状況更新 - {message}"
            thread = await main_message.create_thread(
                name=thread_name, 
                auto_archive_duration=60,
                reason="個人タスク状況詳細"
            )
            
            # スレッドに詳細情報を送信（指示者、状態、作成日時のみ）
            detail_embed = discord.Embed(
                title="📋 詳細情報",
                color=discord.Color.blue()
            )
            detail_embed.add_field(name="指示者", value=instructor.mention, inline=True)
            detail_embed.add_field(name="状態", value=message, inline=True)
            detail_embed.add_field(name="更新日時", value=datetime.datetime.now().strftime("%Y/%m/%d %H:%M"), inline=True)
            
            await thread.send(embed=detail_embed)
            
            # スレッドに適切なユーザーを招待（レート制限対策）
            try:
                # 招待するユーザーのリストを作成（重複を避ける）
                users_to_add = set()
                users_to_add.add(instructor)
                
                # 管理者ロールを持つユーザーを追加
                admin_role = discord.utils.get(guild.roles, name="タスク管理者")
                if admin_role:
                    for member in admin_role.members:
                        users_to_add.add(member)
                
                # 指示者ロールを持つユーザーを追加
                instructor_role = discord.utils.get(guild.roles, name="タスク指示者")
                if instructor_role:
                    for member in instructor_role.members:
                        users_to_add.add(member)
                
                # ユーザーを順次招待（レート制限対策）
                for user in users_to_add:
                    try:
                        await thread.add_user(user)
                        await asyncio.sleep(0.5)  # 0.5秒の遅延
                    except Exception as user_error:
                        logger.error(f"Failed to add user {user.display_name} to thread: {user_error}")
                        continue
                        
            except Exception as e:
                logger.error(f"Failed to add users to thread: {e}")
            
            # 完了の場合、5分後にスレッドを削除するタスクをスケジュール
            if "完了" in message:
                asyncio.create_task(delete_thread_after_delay(thread, 300))  # 5分 = 300秒
        
        # 3. 指示者にDM通知（オプション）
        try:
            embed = discord.Embed(
                title=message,
                color=discord.Color.blue()
            )
            embed.add_field(name="担当者", value=f"<@{assignee_id}>", inline=True)
            embed.add_field(name="タスクID", value=f"#{task_id}", inline=True)
            await instructor.send(embed=embed)
        except discord.Forbidden:
            pass  # DMが無効な場合は無視
        except Exception as dm_error:
            logger.error(f"DM送信エラー: {dm_error}")
    
    except Exception as e:
        logger.error(f"通知送信エラー: {e}")

# 個人チャンネルにタスク通知を送信（修正版）
async def send_task_notification(guild, assignee, instructor, task_name, due_date, original_message_id):
    """個人チャンネルにタスク通知を送信"""
    channel_name = f"{assignee.display_name}のタスク"
    channel = discord.utils.get(guild.channels, name=channel_name)
    
    if not channel:
        # チャンネルが無い場合は自動作成
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                assignee: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                discord.utils.get(guild.roles, name="タスク管理者"): discord.PermissionOverwrite(read_messages=True),
                discord.utils.get(guild.roles, name="タスク指示者"): discord.PermissionOverwrite(read_messages=True)
            }
            
            channel = await guild.create_text_channel(
                channel_name, 
                overwrites=overwrites,
                topic=f"{assignee.display_name}の個人タスク管理チャンネル"
            )
            
            # 初回作成時の説明メッセージ
            welcome_embed = discord.Embed(
                title="📋 個人タスクチャンネル",
                description=f"こんにちは、{assignee.display_name}さん！\nこのチャンネルでタスクの通知を受け取ります。",
                color=discord.Color.blue()
            )
            welcome_embed.add_field(
                name="機能",
                value="• タスク通知の受信\n• タスクの受託・完了報告\n• 進捗状況の確認",
                inline=False
            )
            await channel.send(embed=welcome_embed)
            
        except Exception as e:
            logger.error(f"Failed to create personal channel for {assignee.id}: {e}")
            # チャンネル作成に失敗した場合はDMで送信
            try:
                channel = await assignee.create_dm()
            except:
                return
    else:
        # 既存のチャンネルが見つかった場合、権限を確認・更新
        try:
            # チャンネルの権限を確認し、必要に応じて更新
            current_perms = channel.overwrites_for(assignee)
            if not current_perms.read_messages or not current_perms.send_messages:
                await channel.set_permissions(assignee, read_messages=True, send_messages=True)
                logger.info(f"Updated permissions for {assignee.display_name} in existing channel")
        except Exception as e:
            logger.error(f"Failed to update permissions for {assignee.id}: {e}")
    
    # タスクIDを取得
    result = DatabaseManager.execute_query(
        "SELECT id FROM tasks WHERE guild_id = ? AND assignee_id = ? AND task_name = ? ORDER BY created_at DESC LIMIT 1",
        (guild.id, assignee.id, task_name)
    )
    
    if not result:
        return
    
    task_id = result[0][0]
    
    # メインメッセージ（タスク名と期日のみ）
    embed = discord.Embed(
        title=f"📋 {task_name}",
        description=f"**期日: {due_date.strftime('%Y/%m/%d %H:%M')}**",
        color=discord.Color.gold()
    )
    
    # ビューを作成（初期状態はpending）
    view = TaskView(task_id, assignee.id, instructor.id, "pending")
    
    # メインメッセージを送信
    main_message = await channel.send(f"{assignee.mention}", embed=embed, view=view)
    
    # スレッドを作成して詳細情報を送信
    thread_name = f"📋 {task_name} - 詳細"
    thread = await main_message.create_thread(
        name=thread_name, 
        auto_archive_duration=60,
        reason="タスク詳細情報"
    )
    
    # スレッドに詳細情報を送信（指示者、状態、作成日時のみ）
    detail_embed = discord.Embed(
        title="📋 タスク詳細",
        color=discord.Color.blue()
    )
    detail_embed.add_field(name="指示者", value=instructor.mention, inline=True)
    detail_embed.add_field(name="状態", value="⏳ 未受託", inline=True)
    detail_embed.add_field(name="作成日時", value=datetime.datetime.now().strftime("%Y/%m/%d %H:%M"), inline=True)
    
    await thread.send(embed=detail_embed)
    
    # スレッドに適切なユーザーを招待（レート制限対策）
    try:
        # 招待するユーザーのリストを作成（重複を避ける）
        users_to_add = set()
        users_to_add.add(assignee)
        users_to_add.add(instructor)
        
        # 管理者ロールを持つユーザーを追加
        admin_role = discord.utils.get(guild.roles, name="タスク管理者")
        if admin_role:
            for member in admin_role.members:
                users_to_add.add(member)
        
        # 指示者ロールを持つユーザーを追加
        instructor_role = discord.utils.get(guild.roles, name="タスク指示者")
        if instructor_role:
            for member in instructor_role.members:
                users_to_add.add(member)
        
        # ユーザーを順次招待（レート制限対策）
        for user in users_to_add:
            try:
                await thread.add_user(user)
                await asyncio.sleep(0.5)  # 0.5秒の遅延
            except Exception as user_error:
                logger.error(f"Failed to add user {user.display_name} to thread: {user_error}")
                continue
                
    except Exception as e:
        logger.error(f"Failed to add users to thread: {e}")

# Botイベント
@bot.event
async def on_ready():
    logger.info(f'{bot.user} has landed!')
    logger.info(f"Bot is in {len(bot.guilds)} guilds")
    
    init_database()
    
    # 永続化ビューの設定
    await setup_persistent_views()
    
    # 各ギルドで管理者ロールを作成/更新
    for guild in bot.guilds:
        logger.info(f"Setting up guild: {guild.name} (ID: {guild.id})")
        await setup_roles(guild)
    
    # 定期タスク開始
    try:
        if not check_reminders.is_running():
            check_reminders.start()
            logger.info("Reminder task started successfully")
        
        if not heartbeat_check.is_running():
            heartbeat_check.start()
            logger.info("Heartbeat monitoring started")
        
        if not cleanup_memory.is_running():
            cleanup_memory.start()
            logger.info("Memory cleanup started")
    except Exception as e:
        logger.error(f"Failed to start tasks: {e}")

async def setup_roles(guild):
    """ロールの作成と管理"""
    # タスク管理者ロール
    admin_role = discord.utils.get(guild.roles, name="タスク管理者")
    if not admin_role:
        admin_role = await guild.create_role(
            name="タスク管理者",
            color=discord.Color.red(),
            hoist=True
        )
    
    # タスク指示者ロール
    instructor_role = discord.utils.get(guild.roles, name="タスク指示者")
    if not instructor_role:
        instructor_role = await guild.create_role(
            name="タスク指示者",
            color=discord.Color.blue(),
            hoist=True
        )

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if message.author.bot:
        return
    
    # デバッグログ（簡略化）
    if bot.user in message.mentions:
        logger.info(f"Bot mentioned by {message.author.display_name}: {message.content[:50]}...")
    
    # Bot宛のメンション処理
    if bot.user in message.mentions:
        await handle_task_instruction(message)
    
    await bot.process_commands(message)  # ←これが必須！

async def handle_task_instruction(message):
    """タスク指示の処理"""
    content = message.content
    guild = message.guild
    instructor = message.author
    
    # 権限チェック
    if not (DatabaseManager.is_admin(instructor.id, guild.id) or 
            DatabaseManager.is_instructor(instructor.id, guild.id)):
        await message.reply("❌ 指示権限がありません。管理者にお問い合わせください。")
        return
    
    # メンション解析
    mentions = message.mentions[1:]  # 最初のメンションはBot自身
    if not mentions:
        await message.reply("❌ 指示対象のユーザーをメンションしてください。")
        return
    
    if len(mentions) > 10:
        await message.reply("❌ 一度に指示できるのは最大10人までです。")
        return
    
    # コンテンツから期日とタスク名を抽出
    # 形式: @bot @user1 @user2, 期日, タスク名
    content_parts = content.split(',')
    if len(content_parts) < 3:
        await message.reply("❌ 形式が正しくありません。形式: `@bot @ユーザー, 期日, タスク名`")
        return
    
    date_str = content_parts[1].strip()
    task_name = content_parts[2].strip()
    
    if len(task_name) > 100:
        await message.reply("❌ タスク名は100文字以内で入力してください。")
        return
    
    # 期日解析
    due_date = parse_date(date_str)
    if not due_date:
        await message.reply("❌ 期日は『明日』『12/25』『3日後』『来週』『金曜日』『2時間後』などの形式で入力してください。")
        return
    
    # 各ユーザーにタスクを作成
    success_count = 0
    error_messages = []
    
    for user in mentions:
        # 権限チェック
        if not DatabaseManager.can_instruct_user(instructor.id, user.id, guild.id):
            error_messages.append(f"❌ {user.display_name}への指示権限がありません。")
            continue
        
        # 重複チェック
        if DatabaseManager.check_duplicate_task(user.id, task_name, guild.id):
            error_messages.append(f"❌ {user.display_name}には既に『{task_name}』タスクが指示済みです。")
            continue
        
        # タスク作成
        try:
            # タスクをデータベースに追加
            DatabaseManager.add_task(
                guild.id, instructor.id, user.id, 
                task_name, due_date, message.id, message.channel.id
            )
            
            # タスク通知を個人チャンネルに送信
            await send_task_notification(guild, user, instructor, task_name, due_date, message.id)
            
            success_count += 1
        except Exception as e:
            error_messages.append(f"❌ {user.display_name}: エラーが発生しました。")
            logger.error(f"Task creation error for {user.id}: {e}")
    
    # 結果報告
    result_message = f"✅ {success_count}件のタスクを指示しました。"
    if error_messages:
        result_message += "\n\n⚠️ エラー:\n" + "\n".join(error_messages)
    
    await message.reply(result_message)

# 管理者コマンド
@bot.command(name='セットアップ', aliases=['setup'])
@commands.has_permissions(administrator=True)
async def setup_command(ctx):
    """初期セットアップ"""
    # 重複実行防止
    command_key = f"setup_{ctx.author.id}_{ctx.guild.id}"
    if command_key in executing_commands:
        logger.info(f"Duplicate setup command ignored for {ctx.author.id}")
        return
    
    executing_commands.add(command_key)
    
    try:
        guild = ctx.guild
        author = ctx.author
        
        # 管理者として登録（既存チェック付き）
        was_added = DatabaseManager.add_admin_if_not_exists(author.id, guild.id)
        
        # ロール作成
        await setup_roles(guild)
        
        # 管理者ロールを付与
        admin_role = discord.utils.get(guild.roles, name="タスク管理者")
        if admin_role and admin_role not in author.roles:
            await author.add_roles(admin_role)
        
        embed = discord.Embed(
            title="✅ セットアップ完了",
            description="リマインダーBotの初期設定が完了しました。",
            color=discord.Color.green()
        )
        
        if was_added:
            embed.add_field(
                name="管理者権限",
                value=f"{author.display_name}を管理者に登録しました。",
                inline=False
            )
        else:
            embed.add_field(
                name="管理者権限",
                value=f"{author.display_name}は既に管理者です。",
                inline=False
            )
        
        embed.add_field(
            name="次のステップ",
            value="1. `!チャンネル作成` で通知チャンネルを作成\n2. `!指示者 追加 @ユーザー` で指示権限を付与",
            inline=False
        )
        
        await ctx.send(embed=embed)
        
    finally:
        executing_commands.discard(command_key)

@bot.command(name='管理者', aliases=['admin'])
async def admin_command(ctx, action: str, user: discord.Member):
    """管理者権限管理"""
    # 重複実行防止
    command_key = f"admin_{ctx.author.id}_{user.id}_{ctx.guild.id}_{action}"
    if command_key in executing_commands:
        logger.info(f"Duplicate admin command ignored for {ctx.author.id}")
        return
    
    executing_commands.add(command_key)
    
    try:
        if not DatabaseManager.is_admin(ctx.author.id, ctx.guild.id):
            await ctx.send("❌ 管理者権限が必要です。")
            return
        
        if action == "追加" or action == "add":
            was_added = DatabaseManager.add_admin_if_not_exists(user.id, ctx.guild.id)
            
            if was_added:
                admin_role = discord.utils.get(ctx.guild.roles, name="タスク管理者")
                if admin_role and admin_role not in user.roles:
                    try:
                        await user.add_roles(admin_role)
                        await ctx.send(f"✅ {user.display_name}を管理者に追加しました。")
                    except discord.Forbidden:
                        await ctx.send(f"⚠️ {user.display_name}を管理者に追加しましたが、ロールの付与に失敗しました。（Botの権限を確認してください）")
                else:
                    await ctx.send(f"✅ {user.display_name}を管理者に追加しました。")
            else:
                await ctx.send(f"ℹ️ {user.display_name}は既に管理者です。")
        
        elif action == "削除" or action == "remove":
            # データベースから削除
            DatabaseManager.execute_query(
                "DELETE FROM admins WHERE user_id = ? AND guild_id = ?",
                (user.id, ctx.guild.id)
            )
            
            # Discordロールから削除（権限エラーをハンドリング）
            admin_role = discord.utils.get(ctx.guild.roles, name="タスク管理者")
            if admin_role and admin_role in user.roles:
                try:
                    await user.remove_roles(admin_role)
                    await ctx.send(f"✅ {user.display_name}の管理者権限を削除しました。")
                except discord.Forbidden:
                    await ctx.send(f"⚠️ {user.display_name}の管理者権限を削除しましたが、Discordロールの削除に失敗しました。（Botの権限を確認してください）")
                except Exception as e:
                    logger.error(f"ロール削除エラー: {e}")
                    await ctx.send(f"⚠️ {user.display_name}の管理者権限を削除しましたが、ロール削除中にエラーが発生しました。")
            else:
                await ctx.send(f"✅ {user.display_name}の管理者権限を削除しました。")
            
    finally:
        executing_commands.discard(command_key)

@bot.command(name='指示者', aliases=['instructor'])
async def instructor_command(ctx, action: str, user: discord.Member, *targets):
    """指示権限管理"""
    # 重複実行防止
    command_key = f"instructor_{ctx.author.id}_{user.id}_{ctx.guild.id}_{action}"
    if command_key in executing_commands:
        logger.info(f"Duplicate instructor command ignored for {ctx.author.id}")
        return
    
    executing_commands.add(command_key)
    
    try:
        if not DatabaseManager.is_admin(ctx.author.id, ctx.guild.id):
            await ctx.send("❌ 管理者権限が必要です。")
            return
        
        if action == "追加" or action == "add":
            target_ids = []
            if targets:
                for target in targets:
                    if target.startswith('<@') and target.endswith('>'):
                        target_id = int(target[2:-1].replace('!', ''))
                        target_ids.append(target_id)
            
            was_added = DatabaseManager.add_instructor_if_not_exists(user.id, ctx.guild.id, target_ids)
            
            if was_added:
                instructor_role = discord.utils.get(ctx.guild.roles, name="タスク指示者")
                if instructor_role and instructor_role not in user.roles:
                    await user.add_roles(instructor_role)
                
                target_desc = "全員" if not target_ids else f"{len(target_ids)}人のユーザー"
                await ctx.send(f"✅ {user.display_name}に指示権限を付与しました。（対象: {target_desc}）")
            else:
                await ctx.send(f"ℹ️ {user.display_name}は既に指示者です。")
        
        elif action == "削除" or action == "remove":
            DatabaseManager.execute_query(
                "DELETE FROM instructors WHERE user_id = ? AND guild_id = ?",
                (user.id, ctx.guild.id)
            )
            
            instructor_role = discord.utils.get(ctx.guild.roles, name="タスク指示者")
            if instructor_role and instructor_role in user.roles:
                await user.remove_roles(instructor_role)
            
            await ctx.send(f"✅ {user.display_name}の指示権限を削除しました。")
            
    finally:
        executing_commands.discard(command_key)

@bot.command(name='チャンネル作成', aliases=['channel'])
async def create_channels_command(ctx):
    """通知チャンネル一括作成"""
    if not DatabaseManager.is_admin(ctx.author.id, ctx.guild.id):
        await ctx.send("❌ 管理者権限が必要です。")
        return
    
    guild = ctx.guild
    created_channels = []
    
    # タスク管理チャンネル
    management_channel = discord.utils.get(guild.channels, name="タスク管理")
    if not management_channel:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            discord.utils.get(guild.roles, name="タスク管理者"): discord.PermissionOverwrite(read_messages=True),
            discord.utils.get(guild.roles, name="タスク指示者"): discord.PermissionOverwrite(read_messages=True)
        }
        management_channel = await guild.create_text_channel("タスク管理", overwrites=overwrites)
        created_channels.append("タスク管理")
    
    # 個人タスクチャンネルを全メンバーに作成
    for member in guild.members:
        if member.bot:  # Botは除外
            continue
            
        channel_name = f"{member.display_name}のタスク"
        existing_channel = discord.utils.get(guild.channels, name=channel_name)
        
        if not existing_channel:
            # 権限設定：本人、管理者、指示者のみアクセス可能
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                member: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                discord.utils.get(guild.roles, name="タスク管理者"): discord.PermissionOverwrite(read_messages=True),
                discord.utils.get(guild.roles, name="タスク指示者"): discord.PermissionOverwrite(read_messages=True)
            }
            
            try:
                new_channel = await guild.create_text_channel(
                    channel_name, 
                    overwrites=overwrites,
                    topic=f"{member.display_name}の個人タスク管理チャンネル"
                )
                created_channels.append(channel_name)
                
                # 作成通知をチャンネルに送信
                embed = discord.Embed(
                    title="📋 個人タスクチャンネル",
                    description=f"こんにちは、{member.display_name}さん！\nこのチャンネルでタスクの通知を受け取ります。",
                    color=discord.Color.blue()
                )
                embed.add_field(
                    name="機能",
                    value="• タスク通知の受信\n• タスクの受託・完了報告\n• 進捗状況の確認",
                    inline=False
                )
                await new_channel.send(embed=embed)
                
            except discord.Forbidden:
                logger.warning(f"Permission denied creating channel for {member.display_name}")
            except Exception as e:
                logger.error(f"Error creating channel for {member.display_name}: {e}")
    
    result_message = f"✅ {len(created_channels)}個のチャンネルを作成しました。"
    if created_channels:
        result_message += f"\n作成されたチャンネル: {', '.join(created_channels[:5])}"
        if len(created_channels) > 5:
            result_message += f" ...他{len(created_channels)-5}個"
    else:
        result_message = "ℹ️ 作成する必要のあるチャンネルはありませんでした。"
    
    await ctx.send(result_message)

# 個人チャンネル単体作成コマンドを追加
@bot.command(name='個人チャンネル作成', aliases=['create_personal'])
async def create_personal_channel_command(ctx, user: discord.Member = None):
    """特定ユーザーの個人チャンネルを作成"""
    if not DatabaseManager.is_admin(ctx.author.id, ctx.guild.id):
        await ctx.send("❌ 管理者権限が必要です。")
        return
    
    # ユーザーが指定されていない場合は実行者自身
    if user is None:
        user = ctx.author
    
    if user.bot:
        await ctx.send("❌ Botには個人チャンネルを作成できません。")
        return
    
    guild = ctx.guild
    channel_name = f"{user.display_name}のタスク"
    existing_channel = discord.utils.get(guild.channels, name=channel_name)
    
    if existing_channel:
        # 既存のチャンネルの権限を確認・更新
        try:
            current_perms = existing_channel.overwrites_for(user)
            if not current_perms.read_messages or not current_perms.send_messages:
                await existing_channel.set_permissions(user, read_messages=True, send_messages=True)
                await ctx.send(f"✅ {user.display_name}の既存チャンネルの権限を更新しました: {existing_channel.mention}")
            else:
                await ctx.send(f"ℹ️ {user.display_name}の個人チャンネルは既に存在します: {existing_channel.mention}")
        except Exception as e:
            logger.error(f"Failed to update permissions for existing channel: {e}")
            await ctx.send(f"❌ 既存チャンネルの権限更新に失敗しました: {existing_channel.mention}")
        return
    
    # 権限設定
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        discord.utils.get(guild.roles, name="タスク管理者"): discord.PermissionOverwrite(read_messages=True),
        discord.utils.get(guild.roles, name="タスク指示者"): discord.PermissionOverwrite(read_messages=True)
    }
    
    try:
        new_channel = await guild.create_text_channel(
            channel_name, 
            overwrites=overwrites,
            topic=f"{user.display_name}の個人タスク管理チャンネル"
        )
        
        # 作成通知
        embed = discord.Embed(
            title="📋 個人タスクチャンネル",
            description=f"こんにちは、{user.display_name}さん！\nこのチャンネルでタスクの通知を受け取ります。",
            color=discord.Color.blue()
        )
        embed.add_field(
            name="機能",
            value="• タスク通知の受信\n• タスクの受託・完了報告\n• 進捗状況の確認",
            inline=False
        )
        await new_channel.send(embed=embed)
        
        await ctx.send(f"✅ {user.display_name}の個人チャンネルを作成しました: {new_channel.mention}")
        
    except discord.Forbidden:
        await ctx.send("❌ チャンネル作成権限がありません。")
    except Exception as e:
        logger.error(f"Error creating personal channel: {e}")
        await ctx.send("❌ チャンネル作成中にエラーが発生しました。")

@bot.command(name='タスク一覧', aliases=['tasks'])
async def tasks_command(ctx, scope: str = ""):
    """タスク一覧表示"""
    user_id = ctx.author.id
    guild_id = ctx.guild.id
    
    if scope == "全て" or scope == "all":
        if not (DatabaseManager.is_admin(user_id, guild_id) or DatabaseManager.is_instructor(user_id, guild_id)):
            await ctx.send("❌ 全体表示には権限が必要です。")
            return
        query = "SELECT * FROM tasks WHERE guild_id = ? ORDER BY due_date"
        params = (guild_id,)
    else:
        query = "SELECT * FROM tasks WHERE guild_id = ? AND (instructor_id = ? OR assignee_id = ?) ORDER BY due_date"
        params = (guild_id, user_id, user_id)
    
    tasks = DatabaseManager.execute_query(query, params)
    
    if not tasks:
        await ctx.send("📝 該当するタスクはありません。")
        return
    
    # ページング処理（10件ずつ）
    page_size = 10
    pages = [tasks[i:i + page_size] for i in range(0, len(tasks), page_size)]
    
    for i, page in enumerate(pages):
        embed = discord.Embed(
            title=f"📋 タスク一覧 (ページ {i+1}/{len(pages)})",
            color=discord.Color.blue()
        )
        
        for task in page:
            # データベースの列数に対応
            if len(task) >= 10:
                task_id, guild_id, instructor_id, assignee_id, task_name, due_date, status, created_at, updated_at, message_id = task[:10]
            else:
                task_id, guild_id, instructor_id, assignee_id, task_name, due_date, status, created_at, updated_at = task[:9]
            
            instructor = ctx.guild.get_member(instructor_id)
            assignee = ctx.guild.get_member(assignee_id)
            
            status_emoji = {
                'pending': '⏳',
                'accepted': '✅',
                'completed': '🎉',
                'declined': '❌',
                'abandoned': '⚠️'
            }
            
            embed.add_field(
                name=f"{status_emoji.get(status, '❓')} {task_name}",
                value=f"担当: {assignee.display_name if assignee else 'Unknown'}\n"
                      f"指示者: {instructor.display_name if instructor else 'Unknown'}\n"
                      f"期日: {due_date}\n"
                      f"状態: {status}",
                inline=True
            )
    
    await ctx.send(embed=embed)

@bot.command(name='ヘルプ', aliases=['manual', 'h'])
async def help_command(ctx):
    """ヘルプ表示"""
    # 重複実行防止
    command_key = f"help_{ctx.author.id}_{ctx.guild.id}"
    if command_key in executing_commands:
        logger.info(f"Duplicate help command ignored for {ctx.author.id}")
        return
    
    executing_commands.add(command_key)
    
    try:
        embed = discord.Embed(
            title="🤖 リマインダーBot ヘルプ",
            description="Discord リマインダーBot の使用方法",
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name="📝 タスク指示",
            value="`@bot @ユーザー, 期日, タスク名`\n例: `@bot @田中, 明日, 資料作成`",
            inline=False
        )
        
        embed.add_field(
            name="📋 コマンド一覧",
            value="`!タスク一覧` - 自分のタスク表示\n"
                  "`!タスク一覧 全て` - 全タスク表示（権限者）\n"
                  "`!セットアップ` - 初期設定（管理者）\n"
                  "`!管理者 追加/削除 @ユーザー` - 管理者管理\n"
                  "`!指示者 追加/削除 @ユーザー` - 指示権限付与\n"
                  "`!個人チャンネル作成 @ユーザー` - 個人チャンネル作成",
            inline=False
        )
        
        embed.add_field(
            name="📅 期日指定（大幅拡張）",
            value="**相対指定:** 今日、明日、明後日、3日後、1週間後、2ヶ月後\n"
                  "**時間指定:** 2時間後、30分後\n"
                  "**曜日指定:** 金曜日、来週、来月、月末\n"
                  "**絶対指定:** 12/25、2024/12/25、12月25日\n"
                  "**時間付き:** 明日 14:30、金曜日 18:00",
            inline=False
        )
        
        embed.add_field(
            name="🔧 管理機能",
            value="- 重複チェック機能\n- 権限制御\n- 自動リマインダー（期日1時間前・1回のみ）\n- インタラクティブなタスク管理\n- 個人チャンネル自動作成",
            inline=False
        )
        
        await ctx.send(embed=embed)
        
    finally:
        executing_commands.discard(command_key)

@bot.command(name='テスト', aliases=['test'])
async def test_command(ctx):
    """タスク作成機能のテスト"""
    try:
        # テスト用のタスク作成
        tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
        tomorrow = tomorrow.replace(hour=23, minute=59, second=0, microsecond=0)
        
        # タスクをデータベースに追加
        task_id = DatabaseManager.add_task(
            guild_id=ctx.guild.id,
            instructor_id=ctx.author.id,
            assignee_id=ctx.author.id,
            task_name="テストタスク",
            due_date=tomorrow,
            message_id=ctx.message.id,
            channel_id=ctx.channel.id
        )
        
        if task_id:
            embed = discord.Embed(
                title="🧪 テストタスク作成",
                description="タスク作成機能のテストが成功しました！",
                color=discord.Color.green()
            )
            embed.add_field(name="タスクID", value=str(task_id), inline=True)
            embed.add_field(name="タスク名", value="テストタスク", inline=True)
            embed.add_field(name="期日", value=tomorrow.strftime("%Y/%m/%d %H:%M"), inline=True)
            
            await ctx.send(embed=embed)
        else:
            await ctx.send("❌ タスク作成に失敗しました。")
            
    except Exception as e:
        logger.error(f"Test command error: {e}")
        await ctx.send(f"❌ テスト中にエラーが発生しました: {str(e)}")

@bot.command(name='権限確認', aliases=['perms'])
async def check_permissions(ctx):
    """Botの権限を確認"""
    try:
        bot_member = ctx.guild.get_member(bot.user.id)
        channel = ctx.channel
        
        embed = discord.Embed(
            title="🔍 Bot権限確認",
            description=f"チャンネル: {channel.name}",
            color=discord.Color.blue()
        )
        
        # 重要な権限をチェック
        permissions = [
            ("メッセージを読む", "read_messages"),
            ("メッセージを送信", "send_messages"),
            ("埋め込みリンクを送信", "embed_links"),
            ("ファイルを添付", "attach_files"),
            ("メッセージ履歴を見る", "read_message_history"),
            ("メンションを送信", "mention_everyone"),
            ("ロール管理", "manage_roles")
        ]
        
        for perm_name, perm_attr in permissions:
            has_perm = getattr(channel.permissions_for(bot_member), perm_attr, False)
            status = "✅" if has_perm else "❌"
            embed.add_field(name=f"{status} {perm_name}", value="有効" if has_perm else "無効", inline=True)
        
        # サーバー全体の権限
        embed.add_field(name="\n📋 サーバー権限", value="", inline=False)
        server_perms = [
            ("メンバー管理", "manage_members"),
            ("ロール管理", "manage_roles"),
            ("チャンネル管理", "manage_channels")
        ]
        
        for perm_name, perm_attr in server_perms:
            has_perm = getattr(bot_member.guild_permissions, perm_attr, False)
            status = "✅" if has_perm else "❌"
            embed.add_field(name=f"{status} {perm_name}", value="有効" if has_perm else "無効", inline=True)
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Permission check error: {e}")
        await ctx.send(f"❌ 権限確認中にエラーが発生しました: {str(e)}")

# エラーハンドラー
@bot.event
async def on_command_error(ctx, error):
    """コマンドエラーの処理"""
    if isinstance(error, commands.CommandNotFound):
        return  # コマンドが見つからない場合は無視
    
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ このコマンドを実行する権限がありません。")
    
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ 指定されたユーザーが見つかりません。")
    
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ コマンドの引数が正しくありません。`!ヘルプ`で使用方法を確認してください。")
    
    else:
        logger.error(f"Command error: {error}")
        await ctx.send("❌ コマンドの実行中にエラーが発生しました。")

# ハートビート監視
@tasks.loop(minutes=1)
async def heartbeat_check():
    """接続状態を監視し、必要に応じて再接続"""
    try:
        if not bot.is_ready():
            logger.warning("Bot not ready, attempting to reconnect...")
            return
        
        # 接続状態をログに記録（5分間隔で出力）
        if heartbeat_check.current_loop % 5 == 0:
            logger.info(f"Heartbeat: Bot is online and ready. Latency: {round(bot.latency * 1000)}ms")
        
    except Exception as e:
        logger.error(f"Heartbeat check error: {e}")

# 定期リマインダーチェック（修正版）
@tasks.loop(minutes=5)
async def check_reminders():
    """定期的にリマインダーをチェック（1回のみ送信）"""
    now = datetime.datetime.now()
    
    # 期日1時間前のリマインダー（未送信のもののみ）
    one_hour_later = now + datetime.timedelta(hours=1)
    upcoming_tasks = DatabaseManager.execute_query(
        "SELECT id, guild_id, instructor_id, assignee_id, task_name, due_date FROM tasks WHERE status = 'accepted' AND due_date BETWEEN ? AND ? AND due_date > ? AND reminder_sent = 0",
        (now, one_hour_later, now)
    )
    
    for task in upcoming_tasks:
        task_id, guild_id, instructor_id, assignee_id, task_name, due_date = task
        
        guild = bot.get_guild(guild_id)
        if not guild:
            continue
        
        assignee = guild.get_member(assignee_id)
        if not assignee:
            continue
        
        try:
            embed = discord.Embed(
                title=f"⏰ {task_name}",
                description=f"**期日: {due_date.strftime('%Y/%m/%d %H:%M')}**",
                color=discord.Color.orange()
            )
            
            # 個人チャンネルに送信を試行
            channel_name = f"{assignee.display_name}のタスク"
            channel = discord.utils.get(guild.channels, name=channel_name)
            
            if channel:
                await channel.send(f"{assignee.mention}", embed=embed)
            else:
                # 個人チャンネルがない場合はDMで送信
                await assignee.send(embed=embed)
            
            # リマインダー送信済みフラグを設定
            DatabaseManager.mark_reminder_sent(task_id)
            logger.info(f"Reminder sent to {assignee.id} for task {task_id}")
            
        except discord.Forbidden:
            logger.warning(f"Could not send reminder to {assignee.id}")
            # 送信に失敗してもフラグは立てる（無限リトライを防ぐため）
            DatabaseManager.mark_reminder_sent(task_id)
        except Exception as e:
            logger.error(f"Error sending reminder: {e}")

# 接続管理
@bot.event
async def on_disconnect():
    """Bot切断時の処理"""
    logger.info("Bot disconnected, clearing executing commands")
    executing_commands.clear()

@bot.event
async def on_resumed():
    """Bot再接続時の処理"""
    logger.info("Bot connection resumed")

@bot.event
async def on_error(event, *args, **kwargs):
    """エラーハンドリング"""
    logger.error(f"Error in {event}: {args} {kwargs}")
    
    # 重大なエラーの場合は再起動を検討
    if "rate limit" in str(args).lower() or "timeout" in str(args).lower():
        logger.warning("Rate limit or timeout detected, considering restart...")
    
    # エラー詳細をログに記録
    import traceback
    logger.error(f"Full traceback: {traceback.format_exc()}")

# Botトークンは環境変数から取得してください

# Botトークンは環境変数から取得
if __name__ == "__main__":
    import os
    TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    if not TOKEN:
        logger.error("DISCORD_BOT_TOKEN environment variable not set")
        exit(1)
    
    try:
        logger.info("Bot starting...")
        bot.run(TOKEN)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
        exit(1)

