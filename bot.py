import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from telethon import TelegramClient, events, Button
from telethon.tl import types
from telethon.tl.functions.channels import (
    GetParticipantRequest, 
    JoinChannelRequest,
    GetFullChannelRequest
)
from telethon.tl.functions.messages import (
    GetHistoryRequest,
    ImportChatInviteRequest,
    CheckChatInviteRequest
)
from telethon.errors import (
    ChatAdminRequiredError, ChannelPrivateError, 
    UserNotParticipantError, FloodWaitError,
    ChatWriteForbiddenError, ChatIdInvalidError,
    InviteHashExpiredError, InviteHashInvalidError,
    InviteRequestSentError, UserAlreadyParticipantError,
    ChannelsTooMuchError, ChatInvalidError
)

# ==================== CONFIGURATION ====================
API_ID = '34968593'  # Get from https://my.telegram.org
API_HASH = '07507854a43f550b73d7e2003b688541'  # Get from https://my.telegram.org
BOT_TOKEN = '8269695320:AAFVvdG5tSjavOcFSusKTg6Y0iUeXU8bOM4'  # Get from @BotFather

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
            logger.info("Data saved successfully")
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

# ==================== SIMPLIFIED BOT CORE ====================
class SimpleForwarderBot:
    """Simplified bot with minimal permission checks - focuses on membership only"""
    
    def __init__(self, api_id: str, api_hash: str, bot_token: str):
        self.client = TelegramClient(SESSION_FILE, api_id, api_hash)
        self.bot_token = bot_token
        self.data_manager = DataManager()
        self.active_tasks: Dict[int, asyncio.Task] = {}
        
        # Store message cache to remember forwarded messages
        self.message_cache: Dict[int, types.Message] = {}
        
        # Register event handlers
        self.client.add_event_handler(self.handle_start, events.NewMessage(pattern='/start'))
        self.client.add_event_handler(self.handle_cancel, events.NewMessage(pattern='/cancel'))
        self.client.add_event_handler(self.handle_my_tasks, events.NewMessage(pattern='/mytasks'))
        self.client.add_event_handler(self.handle_stop_task, events.NewMessage(pattern='/stoptask'))
        self.client.add_event_handler(self.handle_status, events.NewMessage(pattern='/status'))
        self.client.add_event_handler(self.handle_help, events.NewMessage(pattern='/help'))
        self.client.add_event_handler(self.handle_message, events.NewMessage())
    
    # ==================== SIMPLIFIED UTILITY METHODS ====================
    
    async def extract_group_info(self, group_input: str) -> Optional[Tuple[int, str, str]]:
        """
        Extract group info from input
        Returns: (group_id, group_title, invite_hash) or None
        """
        try:
            group_input = group_input.strip()
            
            # Check if it's an invite link (private group)
            if 't.me/+' in group_input or 't.me/joinchat/' in group_input:
                # Extract invite hash
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
                        # Check the invite
                        invite = await self.client(CheckChatInviteRequest(invite_hash))
                        
                        if isinstance(invite, types.ChatInvite):
                            return None, invite.title, invite_hash
                        elif isinstance(invite, types.ChatInviteAlready):
                            return invite.chat.id, invite.chat.title, None
                    
                    except Exception as e:
                        logger.error(f"Error checking invite: {e}")
                        return None, None, invite_hash
            
            # Handle regular group/channel link
            elif 't.me/' in group_input:
                # Extract username from link
                match = re.search(r't\.me/([a-zA-Z0-9_]+)', group_input)
                if match:
                    username = match.group(1)
                    try:
                        entity = await self.client.get_entity(username)
                        return abs(entity.id), entity.title, None
                    except Exception as e:
                        logger.error(f"Error getting entity by username: {e}")
                        return None, None, None
            
            # Handle numeric ID
            elif group_input.lstrip('-').isdigit():
                group_id = int(group_input)
                try:
                    entity = await self.client.get_entity(group_id)
                    return abs(entity.id), entity.title, None
                except Exception as e:
                    logger.error(f"Error getting entity by ID: {e}")
                    return None, None, None
            
            return None, None, None
            
        except Exception as e:
            logger.error(f"Error extracting group info: {e}")
            return None, None, None
    
    async def join_private_group(self, invite_hash: str) -> Tuple[bool, str, Optional[int]]:
        """
        Join a private group using invite hash
        Returns: (success, message, chat_id)
        """
        try:
            # Check invite first
            invite = await self.client(CheckChatInviteRequest(invite_hash))
            
            if isinstance(invite, types.ChatInviteAlready):
                return True, f"✅ Already a member of **{invite.chat.title}**", invite.chat.id
            
            elif isinstance(invite, types.ChatInvite):
                # Join the private group
                try:
                    result = await self.client(ImportChatInviteRequest(invite_hash))
                    
                    # Get chat ID from updates
                    for update in result.updates:
                        if hasattr(update, 'chat_id'):
                            chat_id = update.chat_id
                            return True, f"✅ Successfully joined **{invite.title}**", chat_id
                    
                    return False, "❌ Could not get chat ID after joining", None
                    
                except UserAlreadyParticipantError:
                    # Try to get the chat entity
                    try:
                        # Find chat by searching updates
                        async for dialog in self.client.iter_dialogs():
                            if dialog.title == invite.title:
                                return True, f"✅ Already a member of **{invite.title}**", dialog.id
                    except:
                        pass
                    return False, "❌ Already a member but cannot find chat", None
                
                except InviteHashExpiredError:
                    return False, "❌ **Invite link has expired!**", None
                except InviteHashInvalidError:
                    return False, "❌ **Invalid invite link!**", None
                except InviteRequestSentError:
                    return True, "✅ **Join request sent!** Waiting for admin approval.", None
                except ChannelsTooMuchError:
                    return False, "❌ **Bot is in too many groups/channels!**", None
            
            return False, "❌ **Invalid invite link!**", None
            
        except Exception as e:
            logger.error(f"Error joining private group: {e}")
            return False, f"❌ **Error:** {str(e)}", None
    
    async def verify_group_membership(self, group_input: str) -> Tuple[bool, str, Optional[int], Optional[str]]:
        """
        SIMPLIFIED: Only verify bot is a member of the group
        Returns: (success, message, chat_id, chat_title)
        """
        try:
            # Extract group info
            chat_id, chat_title, invite_hash = await self.extract_group_info(group_input)
            
            if invite_hash:
                # It's a private group invite - try to join
                success, message, joined_chat_id = await self.join_private_group(invite_hash)
                
                if not success:
                    return False, message, None, None
                
                if joined_chat_id:
                    chat_id = joined_chat_id
                
                # Get updated chat info
                try:
                    if chat_id:
                        entity = await self.client.get_entity(chat_id)
                        chat_title = entity.title
                except:
                    pass
                
                return True, f"✅ **Group verified:** {chat_title or 'Private Group'}", chat_id, chat_title
            
            elif chat_id:
                # It's a public group/channel
                try:
                    # Try to access the chat
                    entity = await self.client.get_entity(chat_id)
                    
                    # SIMPLIFIED: Just check if we can access it (implicit membership check)
                    try:
                        # Quick access test
                        _ = await self.client.get_entity(chat_id)
                        
                        # Try to get basic info
                        try:
                            participant = await self.client(GetParticipantRequest(
                                channel=entity,
                                participant=await self.client.get_me()
                            ))
                            logger.info(f"Bot is participant in {chat_title}")
                        except:
                            # Not a participant, try to join if public
                            if hasattr(entity, 'username') and entity.username:
                                try:
                                    await self.client(JoinChannelRequest(entity))
                                    logger.info(f"Joined public group: {chat_title}")
                                except Exception as join_error:
                                    return False, f"❌ **Cannot access group:** {str(join_error)}", None, None
                            else:
                                return False, "❌ **Bot is not a member of this private group!**", None, None
                        
                        return True, f"✅ **Group verified:** {chat_title}", chat_id, chat_title
                    
                    except Exception as e:
                        return False, f"❌ **Cannot access group:** {str(e)}", None, None
                
                except Exception as e:
                    logger.error(f"Error accessing group: {e}")
                    return False, f"❌ **Error:** {str(e)}", None, None
            
            else:
                return False, "❌ **Invalid group link or ID!**", None, None
            
        except Exception as e:
            logger.error(f"Error in verify_group_membership: {e}")
            return False, f"❌ **Error:** {str(e)}", None, None
    
    async def forward_message(self, task_data: dict) -> Tuple[bool, str]:
        """
        Forward a message to target chat
        FIXED: Properly handles forwarded messages from user
        """
        try:
            user_id = task_data['user_id']
            source_msg_id = task_data['source_msg_id']
            target_chat_id = task_data['target_chat_id']
            target_chat_title = task_data['target_chat_title']
            
            # Get the message from cache first
            message = self.message_cache.get(source_msg_id)
            
            if not message:
                # Try to get the message from the chat with the user
                try:
                    # Get messages from the chat with the user
                    messages = await self.client.get_messages(user_id, ids=source_msg_id)
                    if not messages:
                        return False, "❌ **Source message not found!**"
                    
                    message = messages
                    # Cache it for future use
                    self.message_cache[source_msg_id] = message
                    logger.info(f"Cached message {source_msg_id} from user {user_id}")
                    
                except Exception as e:
                    logger.error(f"Error getting message {source_msg_id} from user {user_id}: {e}")
                    return False, f"❌ **Cannot access source message:** {str(e)}"
            
            # Check if it's a forwarded message
            if message.forward:
                logger.info(f"Message {source_msg_id} is a forwarded message, forwarding as is...")
            
            # Forward the message
            await self.client.forward_messages(
                entity=target_chat_id,
                messages=message,
                drop_author=False,
                silent=True
            )
            return True, "✅ **Forwarded successfully!**"
        
        except FloodWaitError as e:
            logger.warning(f"Flood wait: {e.seconds} seconds")
            return False, f"⏳ **Flood wait:** {e.seconds} seconds"
        except ChatWriteForbiddenError:
            return False, "❌ **Bot cannot send messages in this chat!**"
        except Exception as e:
            logger.error(f"Forward error: {e}")
            return False, f"❌ **Error:** {str(e)}"
    
    # ==================== TASK MANAGEMENT ====================
    async def start_forwarding_task(self, user_id: int, task_data: dict):
        """
        Start a forwarding task with interval
        FIXED: Uses task_data directly for message access
        """
        task_id = task_data['id']
        interval_hours = task_data['interval']
        target_chat_title = task_data['target_chat_title']
        
        async def task_loop():
            try:
                forward_count = 0
                
                while True:
                    # Forward the message using task_data
                    success, result_msg = await self.forward_message(task_data)
                    forward_count += 1
                    
                    if success:
                        logger.info(f"Task {task_id}: Forward #{forward_count} successful to {target_chat_title}")
                        self.data_manager.update_task_last_forward(user_id, task_id)
                        
                        # Send success notification for first forward
                        if forward_count == 1:
                            try:
                                await self.client.send_message(
                                    user_id,
                                    f"✅ **Task #{task_id} Started Successfully!**\n\n"
                                    f"**Target:** {target_chat_title}\n"
                                    f"**First forward completed!**\n"
                                    f"**Next forward in:** {interval_hours} hour{'s' if interval_hours > 1 else ''}\n\n"
                                    f"🔄 **Task is now running automatically!**",
                                    parse_mode='md'
                                )
                            except:
                                pass
                    else:
                        logger.warning(f"Task {task_id}: Forward #{forward_count} failed - {result_msg}")
                        
                        # Check if it's a flood wait
                        if "Flood wait" in result_msg:
                            try:
                                wait_time_match = re.search(r'(\d+)', result_msg)
                                if wait_time_match:
                                    wait_time = int(wait_time_match.group(1))
                                    logger.info(f"Task {task_id}: Waiting {wait_time} seconds due to flood limit")
                                    await asyncio.sleep(wait_time)
                                    continue
                            except:
                                pass
                        
                        # Send error notification (but don't stop for single error)
                        if forward_count == 1:  # Only on first failure
                            try:
                                await self.client.send_message(
                                    user_id,
                                    f"⚠️ **Task #{task_id} First forward failed:** {result_msg}\n\n"
                                    f"Will retry after {interval_hours} hours.",
                                    parse_mode='md'
                                )
                            except:
                                pass
                        elif forward_count % 5 == 0:  # Periodic error report every 5 failures
                            try:
                                await self.client.send_message(
                                    user_id,
                                    f"⚠️ **Task #{task_id} Periodic Update:**\n"
                                    f"**Failed forwards:** {forward_count}\n"
                                    f"**Last error:** {result_msg}\n\n"
                                    f"Task continues to run...",
                                    parse_mode='md'
                                )
                            except:
                                pass
                    
                    # Wait for next interval
                    logger.info(f"Task {task_id}: Waiting {interval_hours} hours for next forward")
                    await asyncio.sleep(interval_hours * 3600)
                    
            except asyncio.CancelledError:
                logger.info(f"Task {task_id} cancelled by user")
                try:
                    await self.client.send_message(
                        user_id,
                        f"🛑 **Task #{task_id} has been stopped by user request.**",
                        parse_mode='md'
                    )
                except:
                    pass
            except Exception as e:
                logger.error(f"Task {task_id} error: {e}")
                try:
                    await self.client.send_message(
                        user_id,
                        f"❌ **Task #{task_id} Crashed:** {str(e)}\n\n"
                        f"🛑 **Task has been stopped!**",
                        parse_mode='md'
                    )
                except:
                    pass
                finally:
                    # Remove task from active tasks
                    if task_id in self.active_tasks:
                        del self.active_tasks[task_id]
        
        # Create and store the task
        task = asyncio.create_task(task_loop())
        self.active_tasks[task_id] = task
        
        logger.info(f"Started task {task_id} for user {user_id}")
        return task_id
    
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
        
        # Send welcome message
        welcome_text = """
🤖 **Welcome to Simple Auto-Forwarder Bot!** 🤖

📌 **I will forward your messages automatically** with interval control.

🔹 **Simple & Effective:**
✅ Pure forwarding (no modification)
✅ 1-6 hours interval
✅ **Private group support** (via invite links)
✅ **No complex permission checks**
✅ **Handles forwarded messages perfectly**
✅ Robust error handling
✅ **Local data storage only**

📝 **How to use:**
1️⃣ Send **target group/channel** (link, ID, or private invite)
2️⃣ **Forward any message** to me (text, photo, video, etc.)
3️⃣ Choose **interval** (1-6 hours)

🛠 **Supported Inputs:**
• Public group links: `https://t.me/groupname`
• Private group invites: `https://t.me/+invitehash`
• Channel links
• Group/Channel IDs

⚠️ **Important:** 
• Bot must be **added to group** as admin
• Bot needs **'Access to Messages'** permission
• **Never modifies** your content - forwards exactly as is
• **Auto-joins** private groups from invites
• **Perfect for forwarded messages!**

**Commands:**
/cancel - Cancel current operation
/mytasks - View your tasks
/stoptask_1 - Stop task #1
/status - Check bot status
/help - Show this help

🚀 **Let's get started!**

📤 **Step 1:** Send me the **Group/Channel Link, ID, or Private Invite**:
        """
        
        await event.reply(welcome_text, parse_mode='md')
        
        # Set initial state
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
        active_count = 0
        
        for task in tasks:
            task_id = task.get('id', 'N/A')
            interval = task.get('interval', 1)
            target = task.get('target_chat_title', 'Unknown')
            created = task.get('created_at', 'Unknown')[:10]
            status = task.get('status', 'active')
            last_forward = task.get('last_forward', 'Never')
            forward_count = task.get('forward_count', 0)
            
            if isinstance(last_forward, str) and len(last_forward) > 10:
                last_forward = last_forward[:10]
            
            status_emoji = "🟢" if status == 'active' else "🔴"
            if status == 'active':
                active_count += 1
            
            tasks_text += f"**Task #{task_id}** {status_emoji}\n"
            tasks_text += f"• **Target:** {target}\n"
            tasks_text += f"• **Interval:** {interval} hour{'s' if interval > 1 else ''}\n"
            tasks_text += f"• **Created:** {created}\n"
            tasks_text += f"• **Last Forward:** {last_forward}\n"
            tasks_text += f"• **Total Forwards:** {forward_count}\n"
            tasks_text += f"• **Status:** {status.title()}\n"
            tasks_text += f"• **Stop:** `/stoptask_{task_id}`\n\n"
        
        tasks_text += f"📊 **Total Active Tasks:** {active_count}\n"
        tasks_text += "🛑 **To stop a task:** Use `/stoptask_1` (replace 1 with task number)"
        
        await event.reply(tasks_text, parse_mode='md')
    
    async def handle_stop_task(self, event):
        """Handle /stoptask command"""
        user_id = event.sender_id
        command = event.raw_text
        
        # Extract task ID from command
        if '_' in command:
            try:
                task_id = int(command.split('_')[1])
            except:
                await event.reply("❌ **Invalid task ID!**\n\nUse `/stoptask_1` (replace 1 with your task number)", parse_mode='md')
                return
        else:
            await event.reply("❌ **Please specify task ID!**\n\nUse `/stoptask_1` (replace 1 with your task number)", parse_mode='md')
            return
        
        # Stop the task
        stopped = await self.stop_task_by_id(user_id, task_id)
        
        if stopped:
            await event.reply(f"✅ **Task #{task_id} has been stopped!**", parse_mode='md')
        else:
            await event.reply(f"❌ **Task #{task_id} not found or already stopped!**", parse_mode='md')
    
    async def handle_status(self, event):
        """Handle /status command"""
        user_id = event.sender_id
        
        # Get bot info
        me = await self.client.get_me()
        
        # Get tasks info
        tasks = self.data_manager.get_user_tasks(user_id)
        active_tasks = sum(1 for task in tasks if task.get('status') == 'active')
        total_forwards = sum(task.get('forward_count', 0) for task in tasks)
        
        status_text = f"""
🤖 **Bot Status Report**

**Bot Info:**
• **Name:** @{me.username or me.first_name}
• **ID:** {me.id}
• **Status:** 🟢 **Online**
• **Message Cache:** {len(self.message_cache)} messages

**Your Tasks:**
• **Total Tasks:** {len(tasks)}
• **Active Tasks:** {active_tasks}
• **Total Forwards:** {total_forwards}
• **Active Forwarders:** {len(self.active_tasks)}

**System Info:**
• **Storage:** Local JSON file
• **Session:** {SESSION_FILE}
• **Log File:** {LOG_FILE}
• **Data File:** {DATA_FILE}

**Commands Available:**
• /start - Start new task
• /mytasks - View tasks
• /stoptask_1 - Stop task
• /status - This status
• /help - Show help

✅ **Bot is running normally!**
        """
        
        await event.reply(status_text, parse_mode='md')
    
    async def handle_help(self, event):
        """Handle /help command"""
        help_text = """
🆘 **Auto-Forwarder Bot Help**

**Quick Start:**
1. Add bot to your group as **Admin**
2. Grant **"Access to Messages"** permission
3. Use `/start` to begin setup

**How to forward messages:**
1. Use `/start` command
2. Send group/channel link or ID
3. **Forward any message** to the bot
4. Choose interval (1-6 hours)

**Supported Message Types:**
✅ Text messages
✅ Photos & Videos
✅ Documents & Files
✅ **Forwarded messages** (keeps original sender info)
✅ Stickers & GIFs
✅ Voice messages

**Commands:**
• `/start` - Begin new forwarding task
• `/mytasks` - View all your tasks
• `/stoptask_1` - Stop task #1
• `/status` - Check bot status
• `/cancel` - Cancel current operation
• `/help` - Show this help

**Troubleshooting:**
• **Bot not forwarding?** Ensure bot has "Access to Messages"
• **Permission errors?** Bot must be group admin
• **Private groups?** Use invite link format: t.me/+abc123
• **Forwarded messages work perfectly!**

**Important Notes:**
• Bot only **FORWARDS** - never modifies content
• **Keeps original sender info** in forwarded messages
• All data stored **locally** on your machine
• Works 24/7 when bot is running
• Handles flood limits automatically

Need more help? Contact the bot administrator.
        """
        
        await event.reply(help_text, parse_mode='md')
    
    async def handle_message(self, event):
        """Handle all incoming messages"""
        user_id = event.sender_id
        
        # Ignore commands
        if event.raw_text and event.raw_text.startswith('/'):
            return
        
        # Get user state
        state = self.data_manager.get_user_state(user_id)
        
        if not state:
            # User not in any state, send instructions
            await event.reply(
                "👋 **Hello! I'm Simple Auto-Forwarder Bot!** 🤖\n\n"
                "I forward messages automatically with your chosen interval.\n\n"
                "**Perfect for forwarding messages!** ✅\n\n"
                "Type /start to begin or /help for instructions.",
                parse_mode='md'
            )
            return
        
        current_step = state.get('step')
        
        if current_step == 'awaiting_group':
            # Step 1: Get target group/channel
            group_input = event.raw_text.strip()
            
            # Show processing message
            processing_msg = await event.reply("🔍 **Verifying group access...**", parse_mode='md')
            
            # SIMPLIFIED: Just verify group membership/access
            success, message, chat_id, chat_title = await self.verify_group_membership(group_input)
            
            if not success:
                await processing_msg.edit(f"{message}\n\nPlease check the group link and ensure bot is added as admin.")
                return
            
            # Store group info
            state.update({
                'step': 'awaiting_message',
                'target_chat_id': chat_id,
                'target_chat_title': chat_title or "Private Group"
            })
            self.data_manager.set_user_state(user_id, state)
            
            await processing_msg.edit(
                f"✅ **Perfect!** Target set to: **{chat_title or 'Private Group'}**\n\n"
                f"📝 **Step 2:** Now **forward any message** to me that you want to auto-forward.\n\n"
                f"💡 **Tip:** You can forward:\n"
                f"• Text messages\n"
                f"• Photos & Videos\n"
                f"• Documents\n"
                f"• **Any forwarded message** (keeps original info)\n\n"
                f"⚠️ **Important:** I will forward **exactly** what you send, keeping all original information!",
                parse_mode='md'
            )
        
        elif current_step == 'awaiting_message':
            # Step 2: Get message to forward
            if not event.message:
                await event.reply(
                    "❌ **Please send or forward a message!**\n\n"
                    "It can be text, photo, video, document, or any forwarded message.",
                    parse_mode='md'
                )
                return
            
            # Store the message ID and cache it immediately
            message_id = event.message.id
            self.message_cache[message_id] = event.message
            
            # Check if it's a forwarded message
            if event.message.forward:
                logger.info(f"User {user_id} forwarded message {message_id} from {event.message.forward.sender_id}")
            
            # Store the message info
            state['step'] = 'awaiting_interval'
            state['source_msg_id'] = message_id
            state['message_cached'] = True
            self.data_manager.set_user_state(user_id, state)
            
            # Create buttons for interval selection
            buttons = [
                [Button.inline("⏰ 1 Hour", b"int_1"),
                 Button.inline("⏰ 2 Hours", b"int_2"),
                 Button.inline("⏰ 3 Hours", b"int_3")],
                [Button.inline("⏰ 4 Hours", b"int_4"),
                 Button.inline("⏰ 5 Hours", b"int_5"),
                 Button.inline("⏰ 6 Hours", b"int_6")]
            ]
            
            # Show message preview
            message_preview = "📨 **Message received!** "
            if event.message.text:
                preview_text = event.message.text[:50] + "..." if len(event.message.text) > 50 else event.message.text
                message_preview += f"Text: `{preview_text}`"
            elif event.message.photo:
                message_preview += "📷 Photo"
            elif event.message.video:
                message_preview += "🎬 Video"
            elif event.message.document:
                message_preview += "📄 Document"
            elif event.message.forward:
                message_preview += "🔄 Forwarded Message"
            else:
                message_preview += "Media Message"
            
            await event.reply(
                f"{message_preview}\n\n"
                f"⏰ **Step 3:** Choose forwarding interval:\n\n"
                f"**How often should I forward this message?**\n"
                f"Select from 1 to 6 hours:",
                buttons=buttons,
                parse_mode='md'
            )
        
        elif current_step == 'awaiting_interval':
            # Should be handled by button callback
            await event.reply(
                "⏰ **Please select interval using the buttons above!**\n\n"
                "If buttons are missing, type /start to begin again.",
                parse_mode='md'
            )
    
    async def handle_callback(self, event):
        """Handle button callbacks"""
        user_id = event.sender_id
        data = event.data.decode()
        
        # Get user state
        state = self.data_manager.get_user_state(user_id)
        
        if not state or state.get('step') != 'awaiting_interval':
            await event.answer("Session expired. Please type /start to begin again.")
            await event.delete()
            return
        
        if data.startswith('int_') and data[4:].isdigit():
            interval = int(data[4:])
            
            if 1 <= interval <= 6:
                # Complete the setup
                task_data = {
                    'user_id': user_id,
                    'target_chat_id': state['target_chat_id'],
                    'target_chat_title': state['target_chat_title'],
                    'source_msg_id': state['source_msg_id'],
                    'interval': interval,
                    'status': 'active'
                }
                
                # Add task to database
                task_id = self.data_manager.add_forwarding_task(user_id, task_data)
                
                # Start the forwarding task
                await self.start_forwarding_task(user_id, task_data)
                
                # Clear user state
                self.data_manager.clear_user_state(user_id)
                
                # Send confirmation
                confirmation_text = f"""
🎉 **Auto-Forwarding Setup Complete!** 🎉

✅ **Target:** {state['target_chat_title']}
✅ **Interval:** Every {interval} hour{'s' if interval > 1 else ''}
✅ **Task ID:** #{task_id}
✅ **Status:** 🟢 **ACTIVE**

📤 **I will now automatically forward your message** to the target group/channel.

🔄 **First forward will happen immediately**, then every {interval} hour{'s' if interval > 1 else ''}.

📋 **View your tasks:** /mytasks
🛑 **Stop this task:** `/stoptask_{task_id}`
📊 **Check status:** /status
🆕 **Create new task:** /start

💾 **Data stored locally** - No third-party clouds!

⚠️ **Note:** Ensure bot remains running in background for continuous forwarding.
                """
                
                await event.edit(confirmation_text, parse_mode='md')
                
                # Do initial forward immediately
                try:
                    success, result_msg = await self.forward_message(task_data)
                    if success:
                        await asyncio.sleep(1)
                        await event.reply(f"✅ **Initial forward successful!**\n\nNext forward in {interval} hour{'s' if interval > 1 else ''}.", parse_mode='md')
                    else:
                        await event.reply(f"⚠️ **Initial forward attempt:** {result_msg}\n\nWill retry in {interval} hours.", parse_mode='md')
                except Exception as e:
                    await event.reply(f"⚠️ **Initial forward error:** {str(e)}\n\nTask will continue with scheduled intervals.", parse_mode='md')
    
    # ==================== BOT LIFECYCLE ====================
    async def start(self):
        """Start the bot"""
        # Connect to Telegram
        await self.client.start(bot_token=self.bot_token)
        
        # Add callback handler
        self.client.add_event_handler(
            self.handle_callback,
            events.CallbackQuery()
        )
        
        # Load existing tasks
        await self.load_existing_tasks()
        
        # Get bot info
        me = await self.client.get_me()
        logger.info(f"🤖 Simple Bot started as @{me.username}")
        logger.info(f"📱 User ID: {me.id}")
        logger.info(f"🔑 Session: {SESSION_FILE}")
        
        # Send startup notification
        try:
            await self.client.send_message(
                'me',
                f"✅ **Simple Auto-Forwarder Bot Started!**\n\n"
                f"🤖 **Bot:** @{me.username}\n"
                f"🆔 **ID:** {me.id}\n"
                f"📅 **Started:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"💾 **Session:** {SESSION_FILE}\n"
                f"📊 **Data File:** {DATA_FILE}\n"
                f"🔄 **Message Cache:** {len(self.message_cache)} messages\n\n"
                f"🚀 **Ready to forward messages!**\n"
                f"✅ **Perfect for forwarded messages!**",
                parse_mode='md'
            )
        except Exception as e:
            logger.warning(f"Could not send startup message: {e}")
        
        # Keep running
        await self.client.run_until_disconnected()
    
    async def load_existing_tasks(self):
        """Load and restart existing tasks from storage"""
        tasks_data = self.data_manager.data.get('tasks', {})
        loaded_count = 0
        
        for user_id_str, tasks in tasks_data.items():
            user_id = int(user_id_str)
            for task in tasks:
                if task.get('status') == 'active':
                    try:
                        await self.start_forwarding_task(user_id, task)
                        loaded_count += 1
                        logger.info(f"Restarted task {task['id']} for user {user_id}")
                    except Exception as e:
                        logger.error(f"Error restarting task {task['id']}: {e}")
        
        if loaded_count > 0:
            logger.info(f"Loaded {loaded_count} existing tasks")
    
    async def stop(self):
        """Stop the bot gracefully"""
        # Cancel all active tasks
        for task_id, task in self.active_tasks.items():
            task.cancel()
        
        # Save data
        self.data_manager.save_data()
        
        logger.info(f"Bot stopped. Active tasks cancelled: {len(self.active_tasks)}")

# ==================== MAIN ENTRY POINT ====================
async def main():
    """Main function to run the bot"""
    
    print("\n" + "="*60)
    print("🤖 SIMPLE AUTO-FORWARDER BOT")
    print("="*60)
    
    # Create and start bot
    bot = SimpleForwarderBot(API_ID, API_HASH, BOT_TOKEN)
    
    try:
        print(f"🔄 Starting bot with API ID: {API_ID}")
        print(f"🔐 Session file: {SESSION_FILE}")
        print(f"💾 Data file: {DATA_FILE}")
        print(f"📝 Log file: {LOG_FILE}")
        print("="*60)
        print("🚀 Bot is starting... (Press Ctrl+C to stop)")
        print("✅ **Perfect for forwarding messages!**")
        
        await bot.start()
    except KeyboardInterrupt:
        print("\n⚠️ Received interrupt signal - Stopping bot...")
        logger.info("Received keyboard interrupt")
    except Exception as e:
        print(f"\n❌ Bot crashed: {e}")
        logger.error(f"Bot crashed: {e}", exc_info=True)
    finally:
        await bot.stop()
        print("\n✅ Bot stopped gracefully")
        print("="*60)

if __name__ == '__main__':
    # Create data directory if it doesn't exist
    Path(DATA_FILE).parent.mkdir(parents=True, exist_ok=True)
    
    # Run the bot
    asyncio.run(main())
