import asyncio
import json
import logging
import re
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from telethon import TelegramClient, events, Button
from telethon.tl import types
from telethon.tl.functions.channels import (
    GetParticipantRequest, 
    JoinChannelRequest
)
from telethon.tl.functions.messages import (
    ImportChatInviteRequest,
    CheckChatInviteRequest
)
from telethon.errors import (
    ChannelPrivateError, 
    UserNotParticipantError, 
    FloodWaitError,
    ChatWriteForbiddenError, 
    ChatIdInvalidError,
    InviteHashExpiredError, 
    InviteHashInvalidError,
    InviteRequestSentError, 
    UserAlreadyParticipantError,
    ChannelsTooMuchError
)

# ==================== CONFIGURATION ====================
API_ID = os.environ.get('API_ID', '34968593')
API_HASH = os.environ.get('API_HASH', '07507854a43f550b73d7e2003b688541')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '8269695320:AAFVvdG5tSjavOcFSusKTg6Y0iUeXU8bOM4')

# Storage files
DATA_FILE = 'forwarder_data.json'
LOG_FILE = 'forwarder_bot.log'
SESSION_FILE = 'forwarder_bot.session'

# ==================== SETUP LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==================== DATA MANAGEMENT ====================
class DataManager:
    """Manages local storage for bot data"""
    
    def __init__(self, data_file: str = DATA_FILE):
        self.data_file = Path(data_file)
        self.data = self._load_data()
    
    def _load_data(self) -> dict:
        """Load data from JSON file"""
        if self.data_file.exists():
            try:
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Error loading data: {e}")
                return {}
        return {}
    
    def save_data(self):
        """Save data to JSON file"""
        try:
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except IOError as e:
            logger.error(f"Error saving data: {e}")
    
    def get_user_state(self, user_id: int) -> dict:
        """Get user's current state"""
        return self.data.get(str(user_id), {})
    
    def set_user_state(self, user_id: int, state: dict):
        """Set user's state"""
        self.data[str(user_id)] = state
        self.save_data()
    
    def clear_user_state(self, user_id: int):
        """Clear user's state"""
        if str(user_id) in self.data:
            del self.data[str(user_id)]
            self.save_data()
    
    def add_forwarding_task(self, user_id: int, task_data: dict):
        """Add a forwarding task for user"""
        tasks = self.data.setdefault('tasks', {})
        user_tasks = tasks.setdefault(str(user_id), [])
        task_data['id'] = len(user_tasks) + 1
        task_data['created_at'] = datetime.now().isoformat()
        task_data['last_forward'] = datetime.now().isoformat()
        task_data['forward_count'] = 0
        user_tasks.append(task_data)
        self.save_data()
        return task_data['id']
    
    def get_user_tasks(self, user_id: int) -> List[dict]:
        """Get all tasks for a user"""
        return self.data.get('tasks', {}).get(str(user_id), [])
    
    def remove_task(self, user_id: int, task_id: int):
        """Remove a specific task"""
        tasks = self.data.get('tasks', {}).get(str(user_id), [])
        if tasks:
            self.data['tasks'][str(user_id)] = [t for t in tasks if t.get('id') != task_id]
            self.save_data()
    
    def update_task_last_forward(self, user_id: int, task_id: int):
        """Update last forward time and count for task"""
        tasks = self.data.get('tasks', {}).get(str(user_id), [])
        if tasks:
            for task in tasks:
                if task.get('id') == task_id:
                    task['last_forward'] = datetime.now().isoformat()
                    task['forward_count'] = task.get('forward_count', 0) + 1
                    self.save_data()
                    break

# ==================== BOT CORE ====================
class PrivateChatOnlyBot:
    """Bot that only works in private chats with no repeated messages"""
    
    def __init__(self, api_id: str, api_hash: str, bot_token: str):
        self.client = TelegramClient(SESSION_FILE, api_id, api_hash)
        self.bot_token = bot_token
        self.data_manager = DataManager()
        self.active_tasks: Dict[int, asyncio.Task] = {}
        self.message_store: Dict[int, dict] = {}
        
        # Store last message time per user to prevent repeats
        self.user_last_message: Dict[int, float] = {}
        
        # Register event handlers with filters
        self.client.add_event_handler(
            self.handle_start, 
            events.NewMessage(pattern='/start', incoming=True, func=self.is_private_chat)
        )
        self.client.add_event_handler(
            self.handle_cancel, 
            events.NewMessage(pattern='/cancel', incoming=True, func=self.is_private_chat)
        )
        self.client.add_event_handler(
            self.handle_my_tasks, 
            events.NewMessage(pattern='/mytasks', incoming=True, func=self.is_private_chat)
        )
        self.client.add_event_handler(
            self.handle_stop_task, 
            events.NewMessage(pattern='/stoptask', incoming=True, func=self.is_private_chat)
        )
        self.client.add_event_handler(
            self.handle_status, 
            events.NewMessage(pattern='/status', incoming=True, func=self.is_private_chat)
        )
        self.client.add_event_handler(
            self.handle_help, 
            events.NewMessage(pattern='/help', incoming=True, func=self.is_private_chat)
        )
        self.client.add_event_handler(
            self.handle_message,
            events.NewMessage(incoming=True, func=self.is_private_chat_and_not_command)
        )
    
    def is_private_chat(self, event):
        """Check if message is from private chat"""
        return event.is_private
    
    def is_private_chat_and_not_command(self, event):
        """Check if message is from private chat and not a command"""
        if not event.is_private:
            return False
        
        # Check if it's a command
        if event.raw_text and event.raw_text.startswith('/'):
            return False
        
        # Prevent repeated messages from same user
        user_id = event.sender_id
        current_time = datetime.now().timestamp()
        
        # Check if user sent a message recently (within 2 seconds)
        if user_id in self.user_last_message:
            time_diff = current_time - self.user_last_message[user_id]
            if time_diff < 2:  # Less than 2 seconds
                logger.info(f"Ignoring repeated message from user {user_id} within {time_diff:.2f}s")
                return False
        
        # Update last message time
        self.user_last_message[user_id] = current_time
        return True
    
    # ==================== UTILITY METHODS ====================
    
    async def extract_group_info(self, group_input: str) -> Optional[Tuple[int, str, str]]:
        """Extract group info from input"""
        try:
            group_input = group_input.strip()
            
            # Check if it's an invite link
            if 't.me/+' in group_input or 't.me/joinchat/' in group_input:
                patterns = [
                    r't\.me/\+([a-zA-Z0-9_-]+)',
                    r't\.me/joinchat/([a-zA-Z0-9_-]+)',
                    r'https?://t\.me/(?:\+joinchat/|joinchat/)?([a-zA-Z0-9_-]+)'
                ]
                
                invite_hash = None
                for pattern in patterns:
                    match = re.search(pattern, group_input)
                    if match:
                        invite_hash = match.group(1)
                        break
                
                if invite_hash:
                    try:
                        invite = await self.client(CheckChatInviteRequest(invite_hash))
                        if isinstance(invite, types.ChatInvite):
                            return None, invite.title, invite_hash
                        elif isinstance(invite, types.ChatInviteAlready):
                            return invite.chat.id, invite.chat.title, None
                    except:
                        return None, None, invite_hash
            
            # Handle regular group/channel link
            elif 't.me/' in group_input:
                match = re.search(r't\.me/([a-zA-Z0-9_]+)', group_input)
                if match:
                    username = match.group(1)
                    try:
                        entity = await self.client.get_entity(username)
                        return abs(entity.id), entity.title, None
                    except:
                        return None, None, None
            
            # Handle numeric ID
            elif group_input.lstrip('-').isdigit():
                group_id = int(group_input)
                try:
                    entity = await self.client.get_entity(group_id)
                    return abs(entity.id), entity.title, None
                except:
                    return None, None, None
            
            return None, None, None
            
        except Exception as e:
            logger.error(f"Error extracting group info: {e}")
            return None, None, None
    
    async def verify_group_membership(self, group_input: str) -> Tuple[bool, str, Optional[int], Optional[str]]:
        """Verify bot is a member of the group"""
        try:
            chat_id, chat_title, invite_hash = await self.extract_group_info(group_input)
            
            if invite_hash:
                # Private group invite
                try:
                    invite = await self.client(CheckChatInviteRequest(invite_hash))
                    
                    if isinstance(invite, types.ChatInviteAlready):
                        return True, f"✅ Already a member of **{invite.chat.title}**", invite.chat.id, invite.chat.title
                    
                    elif isinstance(invite, types.ChatInvite):
                        try:
                            result = await self.client(ImportChatInviteRequest(invite_hash))
                            for update in result.updates:
                                if hasattr(update, 'chat_id'):
                                    return True, f"✅ Successfully joined **{invite.title}**", update.chat_id, invite.title
                        except UserAlreadyParticipantError:
                            return True, f"✅ Already a member of **{invite.title}**", chat_id, invite.title
                        except InviteRequestSentError:
                            return False, "✅ **Join request sent!** Waiting for admin approval.", None, None
                        except InviteHashExpiredError:
                            return False, "❌ **Invite link has expired!**", None, None
                except Exception as e:
                    logger.error(f"Error joining private group: {e}")
                    return False, f"❌ **Error:** {str(e)}", None, None
            
            elif chat_id:
                # Public group/channel
                try:
                    entity = await self.client.get_entity(chat_id)
                    
                    # Check if we're a participant
                    try:
                        await self.client(GetParticipantRequest(
                            channel=entity,
                            participant=await self.client.get_me()
                        ))
                        return True, f"✅ **Group verified:** {chat_title}", chat_id, chat_title
                    except UserNotParticipantError:
                        # Try to join if public
                        if hasattr(entity, 'username') and entity.username:
                            try:
                                await self.client(JoinChannelRequest(entity))
                                return True, f"✅ **Joined group:** {chat_title}", chat_id, chat_title
                            except Exception as e:
                                return False, f"❌ **Cannot join group:** {str(e)}", None, None
                        else:
                            return False, "❌ **Bot is not a member of this private group!**", None, None
                    
                except Exception as e:
                    return False, f"❌ **Cannot access group:** {str(e)}", None, None
            
            else:
                return False, "❌ **Invalid group link or ID!**", None, None
            
        except Exception as e:
            logger.error(f"Error in verify_group_membership: {e}")
            return False, f"❌ **Error:** {str(e)}", None, None
    
    def store_message_data(self, message: types.Message) -> dict:
        """Store message data"""
        message_data = {
            'id': message.id,
            'text': message.text or message.message,
            'media': None,
            'forward': None,
            'date': message.date.isoformat() if message.date else None,
            'entities': message.entities
        }
        
        if message.media:
            message_data['media'] = {
                'type': type(message.media).__name__,
                'id': getattr(message.media, 'id', None),
            }
        
        if message.forward:
            message_data['forward'] = {
                'sender_name': getattr(message.forward, 'sender_name', None),
                'date': message.forward.date.isoformat() if message.forward.date else None,
            }
        
        self.message_store[message.id] = message_data
        return message_data
    
    async def forward_message(self, task_data: dict) -> Tuple[bool, str]:
        """Forward a message to target chat"""
        try:
            user_id = task_data['user_id']
            source_msg_id = task_data['source_msg_id']
            target_chat_id = task_data['target_chat_id']
            
            # Get the original message
            messages = await self.client.get_messages(user_id, ids=source_msg_id)
            if not messages:
                return False, "❌ **Source message not found!**"
            
            # Forward with original sender preserved
            await self.client.forward_messages(
                entity=target_chat_id,
                messages=messages,
                drop_author=False,  # Preserve original sender
                silent=True
            )
            return True, "✅ **Forwarded successfully!**"
        
        except FloodWaitError as e:
            return False, f"⏳ **Flood wait:** {e.seconds} seconds"
        except ChatWriteForbiddenError:
            return False, "❌ **Bot cannot send messages in this chat!**"
        except Exception as e:
            logger.error(f"Forward error: {e}")
            return False, f"❌ **Error:** {str(e)}"
    
    # ==================== TASK MANAGEMENT ====================
    async def start_forwarding_task(self, user_id: int, task_data: dict):
        """Start a forwarding task with interval"""
        task_id = task_data['id']
        interval_hours = task_data['interval']
        target_chat_title = task_data['target_chat_title']
        
        async def task_loop():
            try:
                forward_count = 0
                
                while True:
                    success, result_msg = await self.forward_message(task_data)
                    forward_count += 1
                    
                    if success:
                        logger.info(f"Task {task_id}: Forward #{forward_count} successful")
                        self.data_manager.update_task_last_forward(user_id, task_id)
                    else:
                        logger.warning(f"Task {task_id}: Forward #{forward_count} failed - {result_msg}")
                    
                    # Wait for next interval
                    await asyncio.sleep(interval_hours * 3600)
                    
            except asyncio.CancelledError:
                logger.info(f"Task {task_id} cancelled")
            except Exception as e:
                logger.error(f"Task {task_id} error: {e}")
        
        # Create and store the task
        task = asyncio.create_task(task_loop())
        self.active_tasks[task_id] = task
        logger.info(f"Started task {task_id} for user {user_id}")
    
    async def stop_task_by_id(self, user_id: int, task_id: int) -> bool:
        """Stop a specific forwarding task"""
        if task_id in self.active_tasks:
            self.active_tasks[task_id].cancel()
            del self.active_tasks[task_id]
            
            # Update task status in storage
            tasks = self.data_manager.get_user_tasks(user_id)
            for task in tasks:
                if task.get('id') == task_id:
                    task['status'] = 'stopped'
                    task['stopped_at'] = datetime.now().isoformat()
                    break
            self.data_manager.save_data()
            
            logger.info(f"Stopped task {task_id} for user {user_id}")
            return True
        return False
    
    # ==================== COMMAND HANDLERS ====================
    async def handle_start(self, event):
        """Handle /start command"""
        user_id = event.sender_id
        
        # Clear any existing state
        self.data_manager.clear_user_state(user_id)
        
        welcome_text = """
🤖 **Welcome to Private Auto-Forwarder Bot!** 🤖

📌 **I will forward your messages automatically** with interval control.

🔹 **Features:**
✅ **Private Chat Only** - Works only in private messages
✅ **No Repeated Messages** - Clean interface
✅ **Preserves original sender** in forwarded messages
✅ 1-6 hours interval
✅ **Private group support** (via invite links)
✅ **Local data storage only**

⚠️ **Important:** 
• **I only work in private chats** (not in groups)
• Bot must be **added to target group** as admin
• Bot needs **'Access to Messages'** permission
• **Never modifies** your content

**Commands:**
/start - Begin new task
/mytasks - View your tasks
/stoptask_1 - Stop task #1
/status - Check bot status
/help - Show help
/cancel - Cancel current operation

🚀 **Let's get started!**

📤 **Step 1:** Send me the **Group/Channel Link, ID, or Private Invite**:
        """
        
        await event.reply(welcome_text, parse_mode='md')
        self.data_manager.set_user_state(user_id, {'step': 'awaiting_group'})
    
    async def handle_cancel(self, event):
        """Handle /cancel command"""
        user_id = event.sender_id
        self.data_manager.clear_user_state(user_id)
        await event.reply("✅ **Operation cancelled!**\n\nType /start to begin again.", parse_mode='md')
    
    async def handle_my_tasks(self, event):
        """Handle /mytasks command"""
        user_id = event.sender_id
        tasks = self.data_manager.get_user_tasks(user_id)
        
        if not tasks:
            await event.reply("📭 **You have no active forwarding tasks!**\n\nUse /start to create one.", parse_mode='md')
            return
        
        tasks_text = "📋 **Your Active Tasks:**\n\n"
        
        for task in tasks:
            task_id = task.get('id', 'N/A')
            interval = task.get('interval', 1)
            target = task.get('target_chat_title', 'Unknown')
            created = task.get('created_at', 'Unknown')[:10]
            status = task.get('status', 'active')
            forward_count = task.get('forward_count', 0)
            
            status_emoji = "🟢" if status == 'active' else "🔴"
            
            tasks_text += f"**Task #{task_id}** {status_emoji}\n"
            tasks_text += f"• **Target:** {target}\n"
            tasks_text += f"• **Interval:** {interval} hour{'s' if interval > 1 else ''}\n"
            tasks_text += f"• **Created:** {created}\n"
            tasks_text += f"• **Forwards:** {forward_count}\n"
            tasks_text += f"• **Stop:** `/stoptask_{task_id}`\n\n"
        
        tasks_text += "🛑 **To stop a task:** Use `/stoptask_1` (replace 1 with task number)"
        
        await event.reply(tasks_text, parse_mode='md')
    
    async def handle_stop_task(self, event):
        """Handle /stoptask command"""
        user_id = event.sender_id
        command = event.raw_text
        
        if '_' in command:
            try:
                task_id = int(command.split('_')[1])
                stopped = await self.stop_task_by_id(user_id, task_id)
                if stopped:
                    await event.reply(f"✅ **Task #{task_id} has been stopped!**", parse_mode='md')
                else:
                    await event.reply(f"❌ **Task #{task_id} not found or already stopped!**", parse_mode='md')
            except:
                await event.reply("❌ **Invalid task ID!**", parse_mode='md')
        else:
            await event.reply("❌ **Please specify task ID!**\n\nUse `/stoptask_1`", parse_mode='md')
    
    async def handle_status(self, event):
        """Handle /status command"""
        user_id = event.sender_id
        me = await self.client.get_me()
        tasks = self.data_manager.get_user_tasks(user_id)
        active_tasks = sum(1 for task in tasks if task.get('status') == 'active')
        
        status_text = f"""
🤖 **Bot Status Report**

**Bot Info:**
• **Name:** @{me.username or me.first_name}
• **ID:** {me.id}
• **Status:** 🟢 **Online**
• **Mode:** Private Chat Only

**Your Tasks:**
• **Total Tasks:** {len(tasks)}
• **Active Tasks:** {active_tasks}

**Bot Restrictions:**
✅ Only works in private chats
✅ No repeated messages
✅ Clean interface

✅ **Bot is running normally!**
        """
        
        await event.reply(status_text, parse_mode='md')
    
    async def handle_help(self, event):
        """Handle /help command"""
        help_text = """
🆘 **Private Auto-Forwarder Bot Help**

**Important: This bot only works in private messages!**

**Quick Start:**
1. **Message me privately** (not in a group)
2. Use `/start` command
3. Send group/channel link or ID
4. Forward your message
5. Choose interval (1-6 hours)

**Why Private Only?**
• Prevents spam in groups
• Cleaner interface
• Better user experience
• No repeated messages

**Commands (Private Chat Only):**
• `/start` - Begin new forwarding task
• `/mytasks` - View all your tasks
• `/stoptask_1` - Stop task #1
• `/status` - Check bot status
• `/help` - Show this help
• `/cancel` - Cancel current operation

**Need help in a group?** Message me privately instead!
        """
        
        await event.reply(help_text, parse_mode='md')
    
    async def handle_message(self, event):
        """Handle all incoming messages (already filtered for private chat)"""
        user_id = event.sender_id
        
        # Get user state
        state = self.data_manager.get_user_state(user_id)
        
        if not state:
            # User not in setup, send minimal response
            await event.reply(
                "👋 **Hello! I'm Private Auto-Forwarder Bot!**\n\n"
                "I only work in private messages.\n\n"
                "Use /start to begin or /help for instructions.",
                parse_mode='md'
            )
            return
        
        current_step = state.get('step')
        
        if current_step == 'awaiting_group':
            group_input = event.raw_text.strip()
            processing_msg = await event.reply("🔍 **Verifying group access...**", parse_mode='md')
            
            success, message, chat_id, chat_title = await self.verify_group_membership(group_input)
            
            if not success:
                await processing_msg.edit(message)
                return
            
            state.update({
                'step': 'awaiting_message',
                'target_chat_id': chat_id,
                'target_chat_title': chat_title or "Private Group"
            })
            self.data_manager.set_user_state(user_id, state)
            
            await processing_msg.edit(
                f"✅ **Target set to:** {chat_title or 'Private Group'}\n\n"
                f"📝 **Step 2:** Forward me the message you want to auto-forward.\n\n"
                f"💡 **Tip:** Forward it (don't copy) to preserve original sender info.",
                parse_mode='md'
            )
        
        elif current_step == 'awaiting_message':
            if not event.message:
                await event.reply("❌ **Please forward a message to me!**", parse_mode='md')
                return
            
            # Store message
            message_data = self.store_message_data(event.message)
            state['step'] = 'awaiting_interval'
            state['source_msg_id'] = event.message.id
            self.data_manager.set_user_state(user_id, state)
            
            # Create buttons for interval selection
            buttons = [
                [Button.inline("1️⃣ 1 Hour", b"int_1"),
                 Button.inline("2️⃣ 2 Hours", b"int_2"),
                 Button.inline("3️⃣ 3 Hours", b"int_3")],
                [Button.inline("4️⃣ 4 Hours", b"int_4"),
                 Button.inline("5️⃣ 5 Hours", b"int_5"),
                 Button.inline("6️⃣ 6 Hours", b"int_6")]
            ]
            
            await event.reply(
                f"✅ **Message received!**\n\n"
                f"⏰ **Step 3:** Choose forwarding interval:",
                buttons=buttons,
                parse_mode='md'
            )
        
        elif current_step == 'awaiting_interval':
            await event.reply(
                "⏰ **Please select interval using the buttons above!**",
                parse_mode='md'
            )
    
    async def handle_callback(self, event):
        """Handle button callbacks"""
        user_id = event.sender_id
        data = event.data.decode()
        
        state = self.data_manager.get_user_state(user_id)
        if not state or state.get('step') != 'awaiting_interval':
            await event.answer("Session expired. Type /start to begin again.")
            await event.delete()
            return
        
        if data.startswith('int_') and data[4:].isdigit():
            interval = int(data[4:])
            
            if 1 <= interval <= 6:
                task_data = {
                    'user_id': user_id,
                    'target_chat_id': state['target_chat_id'],
                    'target_chat_title': state['target_chat_title'],
                    'source_msg_id': state['source_msg_id'],
                    'interval': interval,
                    'status': 'active'
                }
                
                task_id = self.data_manager.add_forwarding_task(user_id, task_data)
                await self.start_forwarding_task(user_id, task_data)
                self.data_manager.clear_user_state(user_id)
                
                confirmation_text = f"""
🎉 **Auto-Forwarding Setup Complete!**

✅ **Target:** {state['target_chat_title']}
✅ **Interval:** Every {interval} hour{'s' if interval > 1 else ''}
✅ **Task ID:** #{task_id}
✅ **Status:** 🟢 **ACTIVE**

📤 **Forwarding will start immediately.**

📋 **View tasks:** /mytasks
🛑 **Stop task:** `/stoptask_{task_id}`
                """
                
                await event.edit(confirmation_text, parse_mode='md')
    
    # ==================== BOT LIFECYCLE ====================
    async def start(self):
        """Start the bot"""
        await self.client.start(bot_token=self.bot_token)
        
        # Add callback handler
        self.client.add_event_handler(
            self.handle_callback,
            events.CallbackQuery()
        )
        
        # Load existing tasks
        await self.load_existing_tasks()
        
        me = await self.client.get_me()
        logger.info(f"🤖 Private Bot started as @{me.username}")
        
        # Keep running
        await self.client.run_until_disconnected()
    
    async def load_existing_tasks(self):
        """Load and restart existing tasks"""
        tasks_data = self.data_manager.data.get('tasks', {})
        
        for user_id_str, tasks in tasks_data.items():
            user_id = int(user_id_str)
            for task in tasks:
                if task.get('status') == 'active':
                    try:
                        await self.start_forwarding_task(user_id, task)
                    except Exception as e:
                        logger.error(f"Error restarting task {task['id']}: {e}")
    
    async def stop(self):
        """Stop the bot gracefully"""
        for task_id, task in self.active_tasks.items():
            task.cancel()
        self.data_manager.save_data()

# ==================== FLASK WEB SERVER FOR REPLIT ====================
from flask import Flask, render_template_string

app = Flask(__name__)
bot_instance = None

# Simple HTML template for Replit
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Telegram Auto-Forwarder Bot</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {
            font-family: Arial, sans-serif;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            color: white;
        }
        .container {
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 40px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
            border: 1px solid rgba(255, 255, 255, 0.2);
        }
        h1 {
            text-align: center;
            margin-bottom: 30px;
            color: white;
            font-size: 2.5em;
        }
        .status {
            background: rgba(0, 255, 0, 0.2);
            padding: 15px;
            border-radius: 10px;
            margin: 20px 0;
            text-align: center;
            font-weight: bold;
            border: 2px solid rgba(0, 255, 0, 0.5);
        }
        .info-box {
            background: rgba(255, 255, 255, 0.1);
            padding: 20px;
            border-radius: 10px;
            margin: 20px 0;
        }
        .feature-list {
            list-style: none;
            padding: 0;
        }
        .feature-list li {
            padding: 10px 0;
            border-bottom: 1px solid rgba(255, 255, 255, 0.2);
        }
        .feature-list li:before {
            content: "✓ ";
            color: #4CAF50;
            font-weight: bold;
        }
        .instructions {
            background: rgba(0, 0, 0, 0.2);
            padding: 25px;
            border-radius: 15px;
            margin: 25px 0;
        }
        .telegram-link {
            display: inline-block;
            background: #0088cc;
            color: white;
            padding: 15px 30px;
            text-decoration: none;
            border-radius: 50px;
            font-weight: bold;
            margin-top: 20px;
            transition: all 0.3s;
        }
        .telegram-link:hover {
            background: #006699;
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(0, 0, 0, 0.3);
        }
        .footer {
            text-align: center;
            margin-top: 40px;
            font-size: 0.9em;
            opacity: 0.8;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🤖 Telegram Auto-Forwarder Bot</h1>
        
        <div class="status">
            🟢 Bot Status: <strong>ONLINE</strong>
        </div>
        
        <div class="info-box">
            <h2>🎯 Key Features:</h2>
            <ul class="feature-list">
                <li><strong>Private Chat Only</strong> - Works only in private messages</li>
                <li><strong>No Repeated Messages</strong> - Clean interface</li>
                <li><strong>Preserves Original Sender</strong> in forwarded messages</li>
                <li><strong>Interval Control</strong> - 1 to 6 hours</li>
                <li><strong>Private Group Support</strong> via invite links</li>
                <li><strong>Local Data Storage</strong> - No third-party clouds</li>
                <li><strong>Auto-Join</strong> private groups</li>
                <li><strong>Error Handling</strong> with automatic retry</li>
            </ul>
        </div>
        
        <div class="instructions">
            <h2>📱 How to Use:</h2>
            <ol>
                <li><strong>Message the bot privately</strong> (not in groups)</li>
                <li>Use <code>/start</code> command</li>
                <li>Send target group/channel link or ID</li>
                <li>Forward your message to the bot</li>
                <li>Choose interval (1-6 hours)</li>
                <li>Done! Bot will forward automatically</li>
            </ol>
            
            <div style="text-align: center;">
                <a href="https://t.me/{{ bot_username }}" class="telegram-link" target="_blank">
                    🚀 Start Chatting with Bot
                </a>
            </div>
        </div>
        
        <div class="info-box">
            <h2>⚙️ Bot Commands:</h2>
            <p><code>/start</code> - Begin new forwarding task</p>
            <p><code>/mytasks</code> - View your active tasks</p>
            <p><code>/stoptask_1</code> - Stop specific task</p>
            <p><code>/status</code> - Check bot status</p>
            <p><code>/help</code> - Show help instructions</p>
            <p><code>/cancel</code> - Cancel current operation</p>
        </div>
        
        <div class="footer">
            <p>🤖 Auto-Forwarder Bot | Made with ❤️ for Telegram</p>
            <p>⚠️ Important: Bot only works in private chats!</p>
        </div>
    </div>
</body>
</html>
"""

@app.route('/')
def home():
    """Home page for Replit deployment"""
    bot_username = "YourBotUsername"  # Will be replaced with actual username
    if bot_instance and bot_instance.client.is_connected():
        try:
            me = bot_instance.client.loop.run_until_complete(bot_instance.client.get_me())
            bot_username = me.username or "auto_forwarder_bot"
        except:
            pass
    
    return render_template_string(HTML_TEMPLATE, bot_username=bot_username)

@app.route('/health')
def health():
    """Health check endpoint for monitoring"""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

# ==================== MAIN ENTRY POINT ====================
async def run_bot():
    """Run the Telegram bot"""
    global bot_instance
    
    print("\n" + "="*60)
    print("🤖 PRIVATE AUTO-FORWARDER BOT")
    print("="*60)
    
    bot_instance = PrivateChatOnlyBot(API_ID, API_HASH, BOT_TOKEN)
    
    try:
        print(f"🔄 Starting bot with API ID: {API_ID}")
        print(f"🔐 Session file: {SESSION_FILE}")
        print(f"💾 Data file: {DATA_FILE}")
        print("="*60)
        print("✅ **Private Chat Only Mode**")
        print("✅ **No Repeated Messages**")
        print("✅ **Flask Web Interface**")
        print("="*60)
        print("🚀 Bot is starting...")
        
        await bot_instance.start()
    except KeyboardInterrupt:
        print("\n⚠️ Stopping bot...")
    except Exception as e:
        print(f"\n❌ Bot crashed: {e}")
        logger.error(f"Bot crashed: {e}", exc_info=True)
    finally:
        await bot_instance.stop()
        print("\n✅ Bot stopped gracefully")

def run_flask():
    """Run Flask web server"""
    port = int(os.environ.get('PORT', 5000))
    print(f"🌐 Starting Flask web server on port {port}")
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    # Create data directory if it doesn't exist
    Path(DATA_FILE).parent.mkdir(parents=True, exist_ok=True)
    
    # Run both bot and web server concurrently
    import threading
    
    # Start Flask in a separate thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Run the bot in main thread
    asyncio.run(run_bot())
