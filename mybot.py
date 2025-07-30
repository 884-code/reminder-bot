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
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Bot設定
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# 重複実行防止用のセット
executing_commands = set()

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
    
    # 既存のテーブルにreminder_sentカラムを追加（存在しない場合）
    try:
        cursor.execute('ALTER TABLE tasks ADD COLUMN reminder_sent INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass  # カラムが既に存在する場合
    
    conn.commit()
    conn.close()
    logger.info("データベース初期化完了")

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
    
    # 相対指定
    if date_part == "今日":
        base_date = now
    elif date_part == "明日":
        base_date = now + datetime.timedelta(days=1)
    elif "日後" in date_part:
        try:
            days = int(date_part.replace("日後", ""))
            base_date = now + datetime.timedelta(days=days)
        except ValueError:
            return None
    elif "週間後" in date_part:
        try:
            weeks = int(date_part.replace("週間後", ""))
            base_date = now + datetime.timedelta(weeks=weeks)
        except ValueError:
            return None
    else:
        # 絶対指定
        base_date = None
        date_patterns = [
            (r'(\d{1,2})/(\d{1,2})', "%m/%d"),
            (r'(\d{4})/(\d{1,2})/(\d{1,2})', "%Y/%m/%d"),
        ]
        
        for pattern, format_str in date_patterns:
            match = re.match(pattern, date_part)
            if match:
                try:
                    if len(match.groups()) == 2:
                        base_date = datetime.datetime.strptime(f"{now.year}/{date_part}", "%Y/%m/%d")
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

# インタラクティブなタスク管理ビュー
class TaskView(discord.ui.View):
    def __init__(self, task_id: int, assignee_id: int, instructor_id: int):
        super().__init__(timeout=None)  # タイムアウトなし
        self.task_id = task_id
        self.assignee_id = assignee_id
        self.instructor_id = instructor_id
    
    @discord.ui.button(label="✅ 受託", style=discord.ButtonStyle.success, custom_id="accept_task")
    async def accept_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.assignee_id:
            await interaction.response.send_message("このタスクの担当者ではありません。", ephemeral=True)
            return
        
        try:
            DatabaseManager.update_task_status(self.task_id, "accepted")
            
            # ボタンを更新
            self.clear_items()
            self.add_item(discord.ui.Button(label="📝 完了報告", style=discord.ButtonStyle.primary, custom_id="complete_task"))
            self.add_item(discord.ui.Button(label="⚠️ 問題発生", style=discord.ButtonStyle.danger, custom_id="abandon_task"))
            
            embed = interaction.message.embeds[0]
            embed.color = discord.Color.blue()
            embed.set_field_at(2, name="状態", value="✅ 受託済み", inline=True)
            
            await interaction.response.edit_message(embed=embed, view=self)
            
        except Exception as e:
            logger.error(f"Error in accept_task: {e}")
            await interaction.response.send_message("エラーが発生しました。", ephemeral=True)
    
    @discord.ui.button(label="❌ 辞退", style=discord.ButtonStyle.secondary, custom_id="decline_task")
    async def decline_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.assignee_id:
            await interaction.response.send_message("このタスクの担当者ではありません。", ephemeral=True)
            return
        
        try:
            DatabaseManager.update_task_status(self.task_id, "declined")
            
            self.clear_items()
            embed = interaction.message.embeds[0]
            embed.color = discord.Color.red()
            embed.set_field_at(2, name="状態", value="❌ 辞退", inline=True)
            
            await interaction.response.edit_message(embed=embed, view=self)
            
        except Exception as e:
            logger.error(f"Error in decline_task: {e}")
            await interaction.response.send_message("エラーが発生しました。", ephemeral=True)

    @discord.ui.button(label="📝 完了報告", style=discord.ButtonStyle.primary, custom_id="complete_task", row=1)
    async def complete_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.assignee_id:
            await interaction.response.send_message("このタスクの担当者ではありません。", ephemeral=True)
            return
        
        try:
            DatabaseManager.update_task_status(self.task_id, "completed")
            
            embed = interaction.message.embeds[0]
            embed.color = discord.Color.green()
            embed.set_field_at(2, name="状態", value="🎉 完了", inline=True)
            
            await interaction.response.edit_message(embed=embed, view=self)
            
            # 通知送信
            guild = interaction.guild
            if guild:
                instructor = guild.get_member(self.instructor_id)
                if instructor:
                    await self.send_notification(guild, f"タスクが完了されました: {embed.title}", instructor, interaction.user.id)
            
        except Exception as e:
            logger.error(f"Error in complete_task: {e}")
            await interaction.response.send_message("エラーが発生しました。", ephemeral=True)

    @discord.ui.button(label="⚠️ 問題発生", style=discord.ButtonStyle.danger, custom_id="abandon_task", row=1)
    async def abandon_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.assignee_id:
            await interaction.response.send_message("このタスクの担当者ではありません。", ephemeral=True)
            return
        
        try:
            DatabaseManager.update_task_status(self.task_id, "abandoned")
            
            self.clear_items()
            embed = interaction.message.embeds[0]
            embed.color = discord.Color.dark_red()
            embed.set_field_at(2, name="状態", value="⚠️ 問題発生", inline=True)
            
            await interaction.response.edit_message(embed=embed, view=self)
            
        except Exception as e:
            logger.error(f"Error in abandon_task: {e}")
            await interaction.response.send_message("エラーが発生しました。", ephemeral=True)
    
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

# 個人チャンネルにタスク通知を送信
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
            await channel.send(embed=welcome_embed)
            
        except Exception as e:
            logger.error(f"Failed to create personal channel for {assignee.id}: {e}")
            return
    
    # タスクIDを取得
    result = DatabaseManager.execute_query(
        "SELECT id FROM tasks WHERE guild_id = ? AND assignee_id = ? AND task_name = ? ORDER BY created_at DESC LIMIT 1",
        (guild.id, assignee.id, task_name)
    )
    
    if not result:
        return
    
    task_id = result[0][0]
    
    # Embed作成
    embed = discord.Embed(
        title=f"📋 新しいタスク: {task_name}",
        description=f"指示者: {instructor.display_name}",
        color=discord.Color.gold()
    )
    embed.add_field(name="期日", value=due_date.strftime("%Y/%m/%d %H:%M"), inline=True)
    embed.add_field(name="指示者", value=instructor.mention, inline=True)
    embed.add_field(name="状態", value="⏳ 未受託", inline=True)
    embed.add_field(name="タスクID", value=f"#{task_id}", inline=True)
    embed.timestamp = datetime.datetime.now()
    
    # ビューを作成
    view = TaskView(task_id, assignee.id, instructor.id)
    
    await channel.send(f"{assignee.mention} 新しいタスクが指示されました！", embed=embed, view=view)

# Botイベント
@bot.event
async def on_ready():
    logger.info(f'{bot.user} が起動しました！')
    logger.info(f"Bot is in {len(bot.guilds)} guilds")
    
    init_database()
    
    # 各ギルドで管理者ロールを作成/更新
    for guild in bot.guilds:
        logger.info(f"Setting up guild: {guild.name} (ID: {guild.id})")
        await setup_roles(guild)
    
    # 定期タスク開始
    try:
        if not check_reminders.is_running():
            check_reminders.start()
            logger.info("Reminder task started successfully")
    except Exception as e:
        logger.error(f"Failed to start reminder task: {e}")

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
    
    # Bot宛のメンション処理
    bot_mentioned = False
    
    if bot.user in message.mentions:
        bot_mentioned = True
    elif bot.user.id in message.raw_mentions:
        bot_mentioned = True
    elif (f"<@{bot.user.id}>" in message.content or 
          f"<@!{bot.user.id}>" in message.content):
        bot_mentioned = True
    
    if bot_mentioned:
        await handle_task_instruction(message)
    
    await bot.process_commands(message)

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
        await message.reply("❌ 期日は『明日』『12/25』『3日後』の形式で入力してください。")
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
        value="1. `!指示者 追加 @ユーザー` で指示権限を付与\n2. `@bot @ユーザー, 期日, タスク名` でタスク指示",
        inline=False
    )
    
    await ctx.send(embed=embed)

@bot.command(name='管理者', aliases=['admin'])
async def admin_command(ctx, action: str, user: discord.Member):
    """管理者権限管理"""
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
        
        # Discordロールから削除
        admin_role = discord.utils.get(ctx.guild.roles, name="タスク管理者")
        if admin_role and admin_role in user.roles:
            try:
                await user.remove_roles(admin_role)
                await ctx.send(f"✅ {user.display_name}の管理者権限を完全に削除しました。")
            except discord.Forbidden:
                await ctx.send(f"⚠️ {user.display_name}の管理者権限を削除しましたが、Discordロールの削除に失敗しました。")
        else:
            await ctx.send(f"✅ {user.display_name}の管理者権限を削除しました。")

@bot.command(name='指示者', aliases=['instructor'])
async def instructor_command(ctx, action: str, user: discord.Member, *targets):
    """指示権限管理"""
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
        # データベースから削除
        DatabaseManager.execute_query(
            "DELETE FROM instructors WHERE user_id = ? AND guild_id = ?",
            (user.id, ctx.guild.id)
        )
        
        # Discordロールから削除
        instructor_role = discord.utils.get(ctx.guild.roles, name="タスク指示者")
        if instructor_role and instructor_role in user.roles:
            try:
                await user.remove_roles(instructor_role)
                await ctx.send(f"✅ {user.display_name}の指示権限を完全に削除しました。")
            except discord.Forbidden:
                await ctx.send(f"⚠️ {user.display_name}の指示権限を削除しましたが、Discordロールの削除に失敗しました。")
        else:
            await ctx.send(f"✅ {user.display_name}の指示権限を削除しました。")

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
              "`!チャンネル作成` - 通知チャンネル一括作成",
        inline=False
    )
    
    embed.add_field(
        name="📅 期日指定",
        value="今日、明日、3日後、1週間後\n12/25、2024/12/25\n時間指定: 明日 14:30",
        inline=False
    )
    
    embed.add_field(
        name="🔧 管理機能",
        value="- 重複チェック機能\n- 権限制御\n- 自動リマインダー（期日1時間前・1回のみ）\n- インタラクティブなタスク管理\n- 個人チャンネル自動作成\n- 完了通知システム",
        inline=False
    )
    
    await ctx.send(embed=embed)

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

# 定期リマインダーチェック
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
                title="⏰ タスク期日リマインダー",
                description=f"『{task_name}』の期日が1時間以内に迫っています。",
                color=discord.Color.orange()
            )
            embed.add_field(name="期日", value=due_date.strftime("%Y/%m/%d %H:%M"), inline=True)
            embed.add_field(name="タスクID", value=f"#{task_id}", inline=True)
            
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

# クリーンアップ処理
@bot.event
async def on_disconnect():
    """Bot切断時の処理"""
    logger.info("Bot disconnected, clearing executing commands")
    executing_commands.clear()

# Botトークンは環境変数から取得
if __name__ == "__main__":
    import os
    TOKEN = os.getenv('DISCORD_BOT_TOKEN')
    if not TOKEN:
        logger.error("DISCORD_BOT_TOKEN environment variable not set")
        exit(1)
    bot.run(TOKEN) 