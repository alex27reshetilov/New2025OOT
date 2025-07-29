import os
import re
import logging
import boto3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CallbackQueryHandler, CommandHandler,
    ContextTypes, filters
)
from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError

# Логування
logging.basicConfig(level=logging.INFO)

# Токен із середовища
TELEGRAM_BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Telegram bot app
app = ApplicationBuilder().token(7334751827:AAExmST813pOdSbTa_Yp40PiMJV4A3CeX6c).build()
response_mode = 'summary'

# ======== AWS Functions ========

def get_access_key_creation_date(session, access_key_id):
    try:
        iam_client = session.client('iam')
        response = iam_client.list_access_keys()
        for key_metadata in response['AccessKeyMetadata']:
            if key_metadata['AccessKeyId'] == access_key_id:
                return key_metadata['CreateDate']
    except Exception as e:
        return f"Error: {str(e)}"

def check_aws_account_and_quotas(access_key_id, secret_access_key):
    try:
        session = boto3.Session(
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key
        )
        sts_client = session.client('sts', region_name='us-east-1')
        sts_client.get_caller_identity()
        quotas = get_ec2_quotas(session)
        created_at = get_access_key_creation_date(session, access_key_id)
        return {
            'alive': True,
            'quotas': quotas,
            'created_at': created_at
        }
    except (NoCredentialsError, PartialCredentialsError, ClientError):
        return {
            'alive': False,
            'quotas': None,
            'created_at': None
        }

def get_ec2_quotas(session, region='us-east-1'):
    try:
        sq_client = session.client('service-quotas', region_name=region)
        on_demand_quota = sq_client.get_service_quota(ServiceCode='ec2', QuotaCode='L-1216C47A')['Quota']['Value']
        spot_quota = sq_client.get_service_quota(ServiceCode='ec2', QuotaCode='L-34B43A08')['Quota']['Value']
        return {'on_demand': on_demand_quota, 'spot': spot_quota}
    except Exception as e:
        return {'error': str(e)}

def parse_accounts(text):
    accounts = []
    entries = re.split(r'\n{2,}', text.strip())
    for entry in entries:
        email = password = access_key = secret_key = ""
        extra_fields = []
        lines = entry.strip().splitlines()
        for i, line in enumerate(lines):
            line = line.strip()
            if re.match(r'^\S+@\S+\.\S+\s+\S+\s+aws\b', line.lower()):
                parts = line.split()
                email = parts[0]
                password = parts[1]
                continue
            if re.match(r'^\S+@\S+\.\S+:\S+$', line):
                email, password = line.split(":", 1)
            elif re.match(r'^\S+@\S+\.\S+\s+\S+$', line):
                parts = line.split()
                email, password = parts[0], parts[1]
            elif re.match(r'^\S+@\S+\.\S+$', line) and not email:
                email = line
                if i + 1 < len(lines) and not password and "@" not in lines[i + 1]:
                    password = lines[i + 1].strip()
            elif line.lower().startswith("access key id:"):
                access_key = line.split(":", 1)[-1].strip()
            elif line.lower().startswith("secret access key:"):
                secret_key = line.split(":", 1)[-1].strip()
            elif re.match(r'^(AKIA|ASIA)[A-Z0-9]{16,}$', line):
                access_key = line
            elif re.match(r'^[A-Za-z0-9/+=]{30,}$', line) and not secret_key:
                secret_key = line
            elif access_key and secret_key:
                extra_fields.append(line)
        if email and access_key and secret_key:
            accounts.append({
                'email': email,
                'password': password,
                'access_key_id': access_key,
                'secret_access_key': secret_key,
                'extra_fields': extra_fields
            })
    return accounts

def find_duplicates(accounts):
    seen_emails = {}
    seen_keys = {}
    duplicates = []
    for i, account in enumerate(accounts, 1):
        email = account['email']
        key = account['access_key_id']
        if email in seen_emails:
            duplicates.append(f"📧 Duplicate email: {email} (lines {seen_emails[email]} and {i})")
        else:
            seen_emails[email] = i
        if key in seen_keys:
            duplicates.append(f"🗝️ Duplicate Access Key ID: {key} (lines {seen_keys[key]} and {i})")
        else:
            seen_keys[key] = i
    return duplicates

# ======== Telegram Handlers ========

async def send_mode_keyboard(update: Update):
    keyboard = [
        [
            InlineKeyboardButton("Mode: List", callback_data='set_summary'),
            InlineKeyboardButton("Mode: Individual", callback_data='set_individual')
        ],
        [
            InlineKeyboardButton("🔁 Check again", callback_data='repeat_check'),
            InlineKeyboardButton("🔍 Check duplicates", callback_data='check_duplicates')
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Choose next action:", reply_markup=reply_markup)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global response_mode
    query = update.callback_query
    await query.answer()
    if query.data == 'set_summary':
        response_mode = 'summary'
        await query.edit_message_text("Mode set: List")
    elif query.data == 'set_individual':
        response_mode = 'individual'
        await query.edit_message_text("Mode set: Individual")
    elif query.data == 'repeat_check':
        await query.edit_message_text("Send new accounts for checking:")
    elif query.data == 'check_duplicates':
        accounts = context.user_data.get('accounts', [])
        if not accounts:
            await query.edit_message_text("Please send accounts first.")
            return
        duplicates = find_duplicates(accounts)
        if not duplicates:
            await query.edit_message_text("✅ No duplicates found.")
        else:
            await query.edit_message_text("⚠️ Duplicates found:\n\n" + "\n".join(duplicates))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    accounts = parse_accounts(text)
    if not accounts:
        await update.message.reply_text("No AWS accounts found. Check the format.")
        return
    context.user_data['accounts'] = accounts
    alive_count = 0
    dead_count = 0
    if response_mode == 'summary':
        reply_lines = []
        for i, account in enumerate(accounts, 1):
            result = check_aws_account_and_quotas(account['access_key_id'], account['secret_access_key'])
            is_alive = result['alive']
            status = "✅ Alive" if is_alive else "❌ Dead"
            if is_alive:
                alive_count += 1
            else:
                dead_count += 1
            line = f"Account {i} ({account['email']}): {status}"
            if is_alive and result['quotas']:
                quotas = result['quotas']
                if 'error' in quotas:
                    line += f"\n⚠️ Quotas not fetched: {quotas['error']}"
                else:
                    line += f"\n - On-demand Quota: {quotas['on_demand']}\n - Spot Quota: {quotas['spot']}"
            created_at = result.get('created_at')
            if created_at:
                line += f"\n📅 Access Key Created: {created_at}"
            reply_lines.append(line)
        reply_lines.append("")
        reply_lines.append(f"🔎 Total checked: {len(accounts)}")
        reply_lines.append(f"✅ Alive: {alive_count}")
        reply_lines.append(f"❌ Dead: {dead_count}")
        await update.message.reply_text("\n\n".join(reply_lines))
    elif response_mode == 'individual':
        for i, account in enumerate(accounts, 1):
            result = check_aws_account_and_quotas(account['access_key_id'], account['secret_access_key'])
            is_alive = result['alive']
            status = "✅ Alive" if is_alive else "❌ Dead"
            if is_alive:
                alive_count += 1
            else:
                dead_count += 1
            extra_info = "\nExtra:\n" + "\n".join(account['extra_fields']) if account['extra_fields'] else ""
            quotas_text = ""
            if is_alive and result['quotas']:
                quotas = result['quotas']
                if 'error' in quotas:
                    quotas_text = f"\n⚠️ Quotas not fetched: {quotas['error']}"
                else:
                    quotas_text = f"\nOn-demand Quota: {quotas['on_demand']}\nSpot Quota: {quotas['spot']}"
            message = (
                f"Account {i}:\n"
                f"📧 Email: {account['email']}\n"
                f"🔑 Password: {account['password']}\n"
                f"🗿 Access Key ID: {account['access_key_id']}\n"
                f"🔐 Secret Access Key: {account['secret_access_key']}{extra_info}\n"
                f"Status: {status}{quotas_text}"
            )
            created_at = result.get('created_at')
            if created_at:
                message += f"\n📅 Access Key Created: {created_at}"
            await update.message.reply_text(message)
        summary = f"🔎 Total checked: {len(accounts)}\n✅ Alive: {alive_count}\n❌ Dead: {dead_count}"
        await update.message.reply_text(summary)
    await send_mode_keyboard(update)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привіт! Надішли список AWS акаунтів у форматі:")

# Register handlers
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.add_handler(CallbackQueryHandler(handle_callback))

# Start polling
if __name__ == "__main__":
    app.run_polling()
