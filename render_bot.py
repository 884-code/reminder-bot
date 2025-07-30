import discord
from discord.ext import commands, tasks
import sqlite3
import os
from datetime import datetime, timedelta
import logging

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Bot設定
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# データベース初期化
def init_db():
    conn = sqlite3.connect('tasks.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            task_name TEXT NOT NULL,
            due_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT DEFAULT 'pending'
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("データベース初期化完了")

# リマインダー機能
@tasks.loop(minutes=5)
async def check_reminders():
    try:
        conn = sqlite3.connect('tasks.db')
        cursor = conn.cursor()
        now = datetime.now()
        cursor.execute('''
            SELECT id, user_id, task_name, due_date
            FROM tasks
            WHERE status = 'pending' AND due_date <= ?
        ''', ((now + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S'),))
        tasks = cursor.fetchall()
        for task_id, user_id, task_name, due_date in tasks:
            try:
                user = await bot.fetch_user(int(user_id))
                await user.send(f"🔔 **タスクリマインダー**\n\n**タスク**: {task_name}\n**期限**: {due_date}\n\n期限が近づいています！")
                logger.info(f"リマインダー送信: {user.name} - {task_name}")
            except Exception as e:
                logger.error(f"リマインダー送信エラー: {e}")
        conn.close()
    except Exception as e:
        logger.error(f"リマインダーチェックエラー: {e}")

@bot.event
async def on_ready():
    logger.info(f'{bot.user} が起動しました！')
    init_db()
    check_reminders.start()

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if bot.user.mentioned_in(message):
        try:
            content = message.content.replace(f'<@{bot.user.id}>', '').strip()
            parts = content.split(',', 1)
            if len(parts) < 2:
                await message.channel.send("❌ 形式: @bot ユーザーID, タスク内容 期日")
                return
            user_mention = parts[0].strip()
            task_info = parts[1].strip()
            if '<' in user_mention and '>' in user_mention:
                user_id = user_mention.replace('<', '').replace('>', '')
            else:
                await message.channel.send("❌ ユーザーIDの形式が正しくありません")
                return
            task_parts = task_info.split()
            if len(task_parts) < 2:
                await message.channel.send("❌ タスク内容と期日を指定してください")
                return
            task_name = ' '.join(task_parts[:-1])
            due_date_str = task_parts[-1]
            try:
                if due_date_str == '明日':
                    due_date = datetime.now() + timedelta(days=1)
                elif due_date_str == '明後日':
                    due_date = datetime.now() + timedelta(days=2)
                elif due_date_str == '来週':
                    due_date = datetime.now() + timedelta(weeks=1)
                else:
                    due_date = datetime.strptime(due_date_str, '%Y-%m-%d')
                conn = sqlite3.connect('tasks.db')
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO tasks (user_id, task_name, due_date, created_at)
                    VALUES (?, ?, ?, ?)
                ''', (user_id, task_name, due_date.strftime('%Y-%m-%d %H:%M:%S'),
                     datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
                conn.commit()
                conn.close()
                await message.channel.send(f"✅ **タスク登録完了**\n\n**ユーザー**: <@{user_id}>\n**タスク**: {task_name}\n**期限**: {due_date.strftime('%Y-%m-%d %H:%M')}")
                logger.info(f"タスク登録: {user_id} - {task_name}")
            except ValueError:
                await message.channel.send("❌ 期日の形式が正しくありません。例: 明日, 明後日, 来週, 2025-07-30")
        except Exception as e:
            logger.error(f"タスク登録エラー: {e}")
            await message.channel.send("❌ タスク登録中にエラーが発生しました")
    await bot.process_commands(message)

@bot.command(name='ヘルプ')
async def help_command(ctx):
    embed = discord.Embed(title="🤖 リマインダくん ヘルプ", color=0x00ff00)
    embed.add_field(name="タスク登録", value="`@bot ユーザーID, タスク内容 期日`\n例: `@bot <@123456789>, レポート作成 明日`", inline=False)
    embed.add_field(name="期日形式", value="• 明日\n• 明後日\n• 来週\n• 2025-07-30", inline=False)
    embed.add_field(name="コマンド", value="• `!ヘルプ` - このヘルプを表示\n• `!ステータス` - Bot状態確認", inline=False)
    await ctx.send(embed=embed)

@bot.command(name='ステータス')
async def status_command(ctx):
    embed = discord.Embed(title="📊 Bot状態", color=0x00ff00)
    embed.add_field(name="接続状態", value="✅ オンライン", inline=True)
    embed.add_field(name="レイテンシ", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="サーバー数", value=len(bot.guilds), inline=True)
    await ctx.send(embed=embed)

if __name__ == "__main__":
    logger.info("Bot を起動中...")
    bot.run(os.getenv('DISCORD_BOT_TOKEN')) 