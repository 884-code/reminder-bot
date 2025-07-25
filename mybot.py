import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import asyncio
import json
import os

# Botの設定
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents)

# タスクデータを保存する辞書（本来はDBを使用）
tasks_db = {}
task_counter = 1

# リマインド頻度の選択肢
REMIND_OPTIONS = {
    '毎朝9:55': '09:55',
    '毎朝10:00': '10:00',
    '毎夕18:00': '18:00',
    '毎日': 'daily',
    '3日おき': '3days',
    '毎週月曜': 'weekly_mon',
    '毎週火曜': 'weekly_tue',
    '毎週水曜': 'weekly_wed',
    '毎週木曜': 'weekly_thu',
    '毎週金曜': 'weekly_fri'
}

class TaskView(discord.ui.View):
    def __init__(self, task_id):
        super().__init__(timeout=None)
        self.task_id = task_id

    @discord.ui.button(label='完了', style=discord.ButtonStyle.green, emoji='✅')
    async def complete_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.task_id in tasks_db:
            tasks_db[self.task_id]['status'] = '完了'
            tasks_db[self.task_id]['completed_at'] = datetime.now()
            
            # 完了通知
            embed = discord.Embed(
                title="タスク完了！",
                description=f"タスク #{self.task_id} が完了しました",
                color=0x00ff00
            )
            embed.add_field(name="タスク内容", value=tasks_db[self.task_id]['content'], inline=False)
            embed.add_field(name="完了者", value=interaction.user.mention, inline=True)
            embed.add_field(name="完了時刻", value=datetime.now().strftime('%Y-%m-%d %H:%M'), inline=True)
            
            await interaction.response.send_message(embed=embed)
            
            # 指示者に通知
            if tasks_db[self.task_id]['assignee_id'] != interaction.user.id:
                assignee = bot.get_user(tasks_db[self.task_id]['assignee_id'])
                if assignee:
                    await assignee.send(f"あなたが指示したタスク #{self.task_id} が完了しました！")

    @discord.ui.button(label='進行中', style=discord.ButtonStyle.primary, emoji='🔄')
    async def in_progress(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.task_id in tasks_db:
            tasks_db[self.task_id]['status'] = '進行中'
            await interaction.response.send_message(f"タスク #{self.task_id} を進行中に更新しました", ephemeral=True)

    @discord.ui.button(label='未着手', style=discord.ButtonStyle.secondary, emoji='⏸️')
    async def not_started(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.task_id in tasks_db:
            tasks_db[self.task_id]['status'] = '未着手'
            await interaction.response.send_message(f"タスク #{self.task_id} を未着手に更新しました", ephemeral=True)

@bot.event
async def on_ready():
    print(f'{bot.user} がログインしました！')
    reminder_loop.start()  # リマインダーループを開始

@bot.command(name='task')
async def create_task(ctx, member: discord.Member, deadline: str, remind_freq: str = '毎朝9:55', *, content: str):
    """
    タスクを作成するコマンド
    使用例: /task @ユーザー 2025-07-30_17:00 毎朝9:55 レポート作成をお願いします
    """
    global task_counter
    
    try:
        # 期日をパース
        deadline_dt = datetime.strptime(deadline, '%Y-%m-%d_%H:%M')
        
        # タスクデータを作成
        task_data = {
            'id': task_counter,
            'content': content,
            'assignee_id': ctx.author.id,
            'target_id': member.id,
            'deadline': deadline_dt,
            'remind_freq': remind_freq,
            'status': '未着手',
            'created_at': datetime.now(),
            'channel_id': ctx.channel.id
        }
        
        tasks_db[task_counter] = task_data
        
        # タスク表示用embed
        embed = discord.Embed(
            title=f"新しいタスク #{task_counter}",
            description=content,
            color=0x3498db
        )
        embed.add_field(name="担当者", value=member.mention, inline=True)
        embed.add_field(name="締切", value=deadline_dt.strftime('%Y年%m月%d日 %H:%M'), inline=True)
        embed.add_field(name="リマインド", value=remind_freq, inline=True)
        embed.add_field(name="状態", value="未着手", inline=True)
        embed.set_footer(text=f"指示者: {ctx.author.display_name}")
        
        # ボタン付きでメッセージ送信
        view = TaskView(task_counter)
        await ctx.send(embed=embed, view=view)
        
        # 担当者にDM送信
        try:
            await member.send(f"新しいタスクが割り当てられました！\n**内容**: {content}\n**締切**: {deadline_dt.strftime('%Y年%m月%d日 %H:%M')}")
        except:
            await ctx.send(f"{member.mention} にDMを送信できませんでした。")
        
        task_counter += 1
        
    except ValueError:
        await ctx.send("期日の形式が正しくありません。YYYY-MM-DD_HH:MM の形式で入力してください。\n例: 2025-07-30_17:00")

@bot.command(name='deadline_change')
async def change_deadline(ctx, task_id: int, new_deadline: str):
    """
    タスクの期日を変更するコマンド
    使用例: /deadline_change 123 2025-07-30_17:00
    """
    if task_id not in tasks_db:
        await ctx.send(f"タスク #{task_id} が見つかりません。")
        return
    
    try:
        new_deadline_dt = datetime.strptime(new_deadline, '%Y-%m-%d_%H:%M')
        old_deadline = tasks_db[task_id]['deadline']
        
        # 権限チェック（指示者または担当者のみ変更可能）
        if ctx.author.id not in [tasks_db[task_id]['assignee_id'], tasks_db[task_id]['target_id']]:
            await ctx.send("このタスクの期日を変更する権限がありません。")
            return
        
        tasks_db[task_id]['deadline'] = new_deadline_dt
        
        embed = discord.Embed(
            title=f"タスク #{task_id} の期日を変更しました",
            color=0xf39c12
        )
        embed.add_field(name="タスク内容", value=tasks_db[task_id]['content'], inline=False)
        embed.add_field(name="変更前", value=old_deadline.strftime('%Y年%m月%d日 %H:%M'), inline=True)
        embed.add_field(name="変更後", value=new_deadline_dt.strftime('%Y年%m月%d日 %H:%M'), inline=True)
        embed.set_footer(text=f"変更者: {ctx.author.display_name}")
        
        await ctx.send(embed=embed)
        
        # 関係者に通知
        assignee = bot.get_user(tasks_db[task_id]['assignee_id'])
        target = bot.get_user(tasks_db[task_id]['target_id'])
        
        for user in [assignee, target]:
            if user and user.id != ctx.author.id:
                try:
                    await user.send(f"タスク #{task_id} の期日が変更されました。\n新しい締切: {new_deadline_dt.strftime('%Y年%m月%d日 %H:%M')}")
                except:
                    pass
        
    except ValueError:
        await ctx.send("期日の形式が正しくありません。YYYY-MM-DD_HH:MM の形式で入力してください。")

@bot.command(name='task_list')
async def list_tasks(ctx):
    """現在のタスク一覧を表示"""
    if not tasks_db:
        await ctx.send("現在登録されているタスクはありません。")
        return
    
    embed = discord.Embed(title="タスク一覧", color=0x2ecc71)
    
    for task_id, task in tasks_db.items():
        if task['status'] != '完了':
            assignee = bot.get_user(task['assignee_id'])
            target = bot.get_user(task['target_id'])
            
            status_emoji = {'未着手': '⏸️', '進行中': '🔄', '完了': '✅'}
            
            embed.add_field(
                name=f"#{task_id} {status_emoji.get(task['status'], '❓')} {task['status']}",
                value=f"**内容**: {task['content']}\n**担当**: {target.display_name if target else 'Unknown'}\n**締切**: {task['deadline'].strftime('%m/%d %H:%M')}",
                inline=False
            )
    
    await ctx.send(embed=embed)

@tasks.loop(minutes=5)  # 5分ごとにチェック
async def reminder_loop():
    """リマインダーをチェックして送信"""
    now = datetime.now()
    current_time = now.strftime('%H:%M')
    current_weekday = now.weekday()  # 0=月曜, 6=日曜
    
    for task_id, task in tasks_db.items():
        if task['status'] == '完了':
            continue
            
        remind_freq = task['remind_freq']
        should_remind = False
        
        # 時刻指定のリマインド（毎朝9:55、毎朝10:00、毎夕18:00など）
        if remind_freq.startswith('毎朝') or remind_freq.startswith('毎夕'):
            if '9:55' in remind_freq and current_time == '09:55':
                should_remind = True
            elif '10:00' in remind_freq and current_time == '10:00':
                should_remind = True
            elif '18:00' in remind_freq and current_time == '18:00':
                should_remind = True
        
        # 毎日（朝9時に送信）
        elif remind_freq == '毎日' and current_time == '09:00':
            should_remind = True
        
        # N日おき
        elif remind_freq.endswith('日おき'):
            try:
                days_interval = int(remind_freq.replace('日おき', ''))
                last_reminded = task.get('last_reminded')
                
                if last_reminded is None:
                    # 初回は即座にリマインド
                    should_remind = True
                else:
                    # 指定日数経過したかチェック
                    days_since_last = (now - last_reminded).days
                    if days_since_last >= days_interval and current_time == '09:00':
                        should_remind = True
            except:
                pass
        
        # 毎週○曜日
        elif remind_freq.startswith('毎週'):
            weekday_map = {
                '毎週月曜': 0, '毎週火曜': 1, '毎週水曜': 2, 
                '毎週木曜': 3, '毎週金曜': 4, '毎週土曜': 5, '毎週日曜': 6
            }
            
            if remind_freq in weekday_map:
                target_weekday = weekday_map[remind_freq]
                if current_weekday == target_weekday and current_time == '09:00':
                    should_remind = True
        
        # リマインド送信
        if should_remind:
            await send_reminder(task_id, task)
            # 最後にリマインドした時刻を更新
            tasks_db[task_id]['last_reminded'] = now
        
        # 期日直前のリマインド（1日前、1時間前）
        time_until_deadline = task['deadline'] - now
        
        if timedelta(hours=23, minutes=55) <= time_until_deadline <= timedelta(hours=24, minutes=5):
            await send_reminder(task_id, task, "⚠️ 期日まで24時間を切りました！")
        elif timedelta(minutes=55) <= time_until_deadline <= timedelta(hours=1, minutes=5):
            await send_reminder(task_id, task, "🚨 期日まで1時間を切りました！")

async def send_reminder(task_id, task, extra_message=""):
    """リマインダーを個人用チャンネルに送信"""
    target = bot.get_user(task['target_id'])
    if not target:
        return
    
    embed = discord.Embed(
        title=f"📋 タスクリマインダー #{task_id}",
        description=extra_message,
        color=0xe74c3c if extra_message else 0x3498db
    )
    embed.add_field(name="タスク内容", value=task['content'], inline=False)
    embed.add_field(name="締切", value=task['deadline'].strftime('%Y年%m月%d日 %H:%M'), inline=True)
    embed.add_field(name="現在の状態", value=task['status'], inline=True)
    
    # 個人用チャンネルを探す（例：ユーザー名-personal のような命名規則）
    personal_channel = None
    
    # サーバー内のチャンネルを検索
    for guild in bot.guilds:
        for channel in guild.text_channels:
            # 個人用チャンネルの命名パターンを確認
            if (channel.name == f"{target.display_name.lower()}-personal" or 
                channel.name == f"personal-{target.display_name.lower()}" or
                f"{target.display_name.lower()}" in channel.name and "personal" in channel.name):
                personal_channel = channel
                break
        if personal_channel:
            break
    
    # 個人用チャンネルが見つかった場合はそこに送信
    if personal_channel:
        try:
            await personal_channel.send(embed=embed)
            return
        except:
            pass
    
    # 個人用チャンネルが見つからない場合はDMに送信
    try:
        await target.send(embed=embed)
    except:
        # DMも送信できない場合は元のチャンネルに送信
        channel = bot.get_channel(task['channel_id'])
        if channel:
            await channel.send(f"{target.mention} 個人用チャンネルが見つからないため、こちらに送信します。", embed=embed)

# Botを起動（実際の使用時はトークンを設定）
if __name__ == "__main__":
    bot.run(os.environ['DISCORD_BOT_TOKEN'])