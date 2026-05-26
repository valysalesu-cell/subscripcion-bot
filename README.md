# Telegram Renewal Management Bot

Production-ready Telegram renewal bot built with Python 3.11, aiogram 3, Supabase, and Railway. It uses long polling, not webhooks.

## Features

- Admin-only `/send_poll` command that sends a CTA message with one inline button to `CONTENT_CHANNEL_ID`.
- Captures CTA button clicks and stores or updates users in Supabase.
- Admin-only `/users` command with total registered users and latest 10 users.
- Admin-only `/set_expiry <telegram_id> <YYYY-MM-DD>` command.
- Admin-only `/expired` command that lists expired users without removing anyone.
- Admin-only `/sync_schema` command that safely adds missing dashboard lifecycle columns when a Supabase `exec_sql` RPC is available.
- Daily scheduled notification to `ADMIN_CHAT_ID` for users expiring today.
- Secure FastAPI admin dashboard with password login, filters, renewal actions, one-use invite links, and channel removal.
- Structured logging and defensive error handling.

## Supabase schema

Create this table in Supabase SQL editor:

```sql
create table if not exists telegram_users (
  telegram_id bigint primary key,
  username text,
  first_name text,
  last_name text,
  status text,
  notes text,
  registered_at timestamptz not null default now(),
  joined_at timestamptz,
  membership_start_date date,
  expiry_date date,
  payment_status text default 'unpaid',
  pending_payment_file_id text,
  pending_payment_file_type text,
  pending_payment_at timestamptz,
  approved_by_admin_id bigint,
  approved_at timestamptz,
  rejected_at timestamptz,
  needs_new_receipt_at timestamptz,
  last_payment_at timestamptz,
  invite_link text,
  invite_link_created_at timestamptz,
  invite_link_name text,
  invite_link_revoked boolean default false,
  invite_link_used boolean default false,
  revoked_at timestamptz,
  joined_channel_at timestamptz,
  left_channel_at timestamptz,
  last_seen_at timestamptz,
  renewal_notice_7d_sent_at timestamptz,
  renewal_notice_3d_sent_at timestamptz,
  renewal_notice_1d_sent_at timestamptz,
  removed_at timestamptz,
  removal_reason text,
  confirmed_subscription boolean default false,
  confirmed_at timestamptz,
  confirmation_campaign text,
  source text
);

create index if not exists telegram_users_registered_at_idx
  on telegram_users (registered_at desc);

create index if not exists telegram_users_expiry_date_idx
  on telegram_users (expiry_date);

create table if not exists payment_history (
  id bigserial primary key,
  telegram_id bigint not null,
  username text,
  first_name text,
  admin_id bigint,
  action text default 'approved',
  payment_status text default 'paid',
  receipt_file_id text,
  receipt_file_type text,
  invite_link text,
  membership_start_date date,
  expiry_date date,
  verified boolean default true,
  notes text,
  created_at timestamptz default now()
);

create index if not exists payment_history_telegram_id_idx
  on payment_history (telegram_id);

create index if not exists payment_history_created_at_idx
  on payment_history (created_at desc);

create index if not exists payment_history_payment_status_idx
  on payment_history (payment_status);
```

For existing tables, `/sync_schema` and startup migration attempt to run:

```sql
alter table public.telegram_users add column if not exists joined_at timestamptz;
alter table public.telegram_users add column if not exists membership_start_date date;
alter table public.telegram_users add column if not exists payment_status text default 'unpaid';
alter table public.telegram_users add column if not exists pending_payment_file_id text;
alter table public.telegram_users add column if not exists pending_payment_file_type text;
alter table public.telegram_users add column if not exists pending_payment_at timestamptz;
alter table public.telegram_users add column if not exists approved_by_admin_id bigint;
alter table public.telegram_users add column if not exists approved_at timestamptz;
alter table public.telegram_users add column if not exists rejected_at timestamptz;
alter table public.telegram_users add column if not exists needs_new_receipt_at timestamptz;
alter table public.telegram_users add column if not exists last_payment_at timestamptz;
alter table public.telegram_users add column if not exists invite_link text;
alter table public.telegram_users add column if not exists invite_link_created_at timestamptz;
alter table public.telegram_users add column if not exists invite_link_name text;
alter table public.telegram_users add column if not exists invite_link_revoked boolean default false;
alter table public.telegram_users add column if not exists invite_link_used boolean default false;
alter table public.telegram_users add column if not exists revoked_at timestamptz;
alter table public.telegram_users add column if not exists joined_channel_at timestamptz;
alter table public.telegram_users add column if not exists left_channel_at timestamptz;
alter table public.telegram_users add column if not exists last_seen_at timestamptz;
alter table public.telegram_users add column if not exists renewal_notice_7d_sent_at timestamptz;
alter table public.telegram_users add column if not exists renewal_notice_3d_sent_at timestamptz;
alter table public.telegram_users add column if not exists renewal_notice_1d_sent_at timestamptz;
alter table public.telegram_users add column if not exists removed_at timestamptz;
alter table public.telegram_users add column if not exists removal_reason text;
alter table public.telegram_users add column if not exists confirmed_subscription boolean default false;
alter table public.telegram_users add column if not exists confirmed_at timestamptz;
alter table public.telegram_users add column if not exists confirmation_campaign text;
alter table public.telegram_users add column if not exists source text;
alter table public.telegram_users add column if not exists status text;
alter table public.telegram_users add column if not exists notes text;
alter table public.telegram_users add column if not exists expiry_date date;
alter table public.telegram_users alter column payment_status set default 'unpaid';
alter table public.telegram_users alter column confirmed_subscription set default false;
alter table public.telegram_users alter column invite_link_revoked set default false;
alter table public.telegram_users alter column invite_link_used set default false;
update public.telegram_users
set joined_at = coalesce(joined_at, registered_at, now())
where joined_at is null;
update public.telegram_users
set payment_status = coalesce(payment_status, 'unpaid')
where payment_status is null;
update public.telegram_users
set confirmed_subscription = coalesce(confirmed_subscription, false)
where confirmed_subscription is null;
update public.telegram_users
set invite_link_revoked = coalesce(invite_link_revoked, false)
where invite_link_revoked is null;
update public.telegram_users
set invite_link_used = coalesce(invite_link_used, false)
where invite_link_used is null;
update public.telegram_users
set expiry_date = (joined_at + interval '30 days')::date
where expiry_date is null and membership_start_date is null and joined_at is not null;
update public.telegram_users
set expiry_date = membership_start_date + 30
where expiry_date is null and membership_start_date is not null;
create table if not exists public.payment_history (
  id bigserial primary key,
  telegram_id bigint not null,
  username text,
  first_name text,
  admin_id bigint,
  action text default 'approved',
  payment_status text default 'paid',
  receipt_file_id text,
  receipt_file_type text,
  invite_link text,
  membership_start_date date,
  expiry_date date,
  verified boolean default true,
  notes text,
  created_at timestamptz default now()
);
alter table public.payment_history add column if not exists receipt_file_type text;
alter table public.payment_history add column if not exists membership_start_date date;
alter table public.payment_history add column if not exists expiry_date date;
alter table public.payment_history add column if not exists verified boolean default true;
alter table public.payment_history alter column action set default 'approved';
alter table public.payment_history alter column payment_status set default 'paid';
alter table public.payment_history alter column verified set default true;
create index if not exists payment_history_telegram_id_idx
  on public.payment_history (telegram_id);
create index if not exists payment_history_created_at_idx
  on public.payment_history (created_at desc);
create index if not exists payment_history_payment_status_idx
  on public.payment_history (payment_status);
```

Supabase REST does not expose DDL by default. To let the bot run this automatically, create a tightly controlled `exec_sql` RPC for your server-side service role, or run the SQL manually in the Supabase SQL editor.

Use the Supabase service role key only on Railway/server-side infrastructure. Never expose it in client code.

## Telegram setup

1. Create a bot with BotFather and copy the token.
2. Add the bot to your content channel.
3. Promote the bot to admin in the channel so it can send messages.
4. Give the bot permission to invite users, ban users, and receive member updates for invite/removal and join/leave tracking.
5. Disable privacy mode if you later need group command behavior.
6. Get your numeric Telegram admin user ID and add it to `ADMIN_USER_IDS`.

## Environment variables

Set these in Railway:

```bash
BOT_TOKEN=123456:telegram-token
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
ADMIN_CHAT_ID=123456789
CONTENT_CHANNEL_ID=-1001234567890
ADMIN_USER_IDS=123456789,987654321
ADMIN_PASSWORD=use-a-long-random-password
AUTO_REMOVE_EXPIRED=false
RENEWAL_NOTICE_DAYS=7,3,1
```

`CONTENT_CHANNEL_ID` can be a numeric channel ID or a public `@channelusername`.

## Local development

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Optional local `.env` files are supported through `python-dotenv`.

The local web dashboard runs on `http://localhost:8080` unless `PORT` is set.

## Commands

```text
/send_poll
/send_confirm_subscription
/users
/pending_payments
/user <telegram_id>
/payment_history <telegram_id>
/send_invite <telegram_id>
/revoke_invite <telegram_id>
/revoke_user <telegram_id>
/revoke_link <invite_link_name>
/approve <telegram_id>
/reject <telegram_id>
/ask_receipt <telegram_id>
/set_expiry <telegram_id> <YYYY-MM-DD>
/expired
/remove_expired_preview
/remove_expired_confirm
/unconfirmed
/sync_schema
```

Only users listed in `ADMIN_USER_IDS` can run admin commands.

## Web dashboard

Open `/login` and sign in with `ADMIN_PASSWORD`. The dashboard is available at `/dashboard`.

Dashboard columns:

- `telegram_id`
- `username`
- `first_name`
- `payment_status`
- `status`
- `confirmed_subscription`
- `confirmed_at`
- `source`
- Registered / Join Date
- `membership_start_date`
- `expiry_date`
- days remaining
- `joined_channel_at`
- `left_channel_at`
- `invite_link`
- `notes`
- latest 5 payment history records per user row

Dashboard filters:

- All
- Pending payments
- Paid
- Needs new receipt
- Rejected
- Active
- Confirmed
- Not confirmed
- Source: confirm_subscription_button
- Expiring in 7 days
- Expired
- No expiry date
- Has payment history
- Removed/inactive

Dashboard actions:

- Approve pending payment
- Reject payment
- Ask for another receipt
- Renew +30 days from today
- Renew +30 days from current expiry date if still active
- Set membership start date
- Mark paid
- Mark confirmed manually
- Mark not confirmed
- Mark inactive
- Generate one-use invite link using Telegram Bot API for `CONTENT_CHANNEL_ID`
- Send existing invite link
- Revoke current invite
- Remove from channel using a confirmation page, then Telegram ban/unban

The dashboard stores signed session cookies and does not expose the Supabase service role key to the browser.

## Payment approval and renewal jobs

Users send payment receipts to the bot in private chat as a photo or document. The bot marks them `pending_review` and sends admin buttons to `ADMIN_CHAT_ID`. If a user sends another receipt while still pending review, the bot replaces the stored receipt reference and does not send another admin alert or create another approval button. Invite links are generated and sent only after an admin approves the payment.

Payment history:

- Only approved payments are appended to `payment_history`.
- Pending receipts, rejected receipts, requests for another capture, and invite revocations are not stored as payment history.
- Receipt file IDs are copied into payment history only for approved payments so the dashboard can show a protected screenshot preview.
- Payment history writes are best-effort: if the history table is unavailable, the main payment flow continues and the bot logs a warning.
- Use `/payment_history <telegram_id>`, `/dashboard/users/{telegram_id}/history`, or `/dashboard/payments` to review approved payment history.

Invite security:

- Approval reuses an existing active unused invite link instead of creating duplicates.
- The dashboard refuses to generate another link while a user already has an active unused link.
- Use `Revoke current invite`, `/revoke_user <telegram_id>`, or `/revoke_link <invite_link_name>` before generating a replacement.
- Invite links use `member_limit=1` and expire after one hour.
- When Telegram reports the user joined the channel, `invite_link_used` is marked `true`.
- Recent duplicate approvals are blocked with a warning instead of generating another link.

The bot runs an in-process scheduler while long polling is active. It sends daily renewal notices to `ADMIN_CHAT_ID` at `09:00 America/Mexico_City` for the days in `RENEWAL_NOTICE_DAYS`, includes expired users, and only removes expired active users when `AUTO_REMOVE_EXPIRED=true`.

## Testing checklist

1. Run `/sync_schema` or execute the SQL migration above in Supabase.
2. Send a photo or PDF receipt to the bot in a private chat from a non-admin account.
3. Confirm `ADMIN_CHAT_ID` receives the pending payment message with approval buttons.
4. Click `Aprobar ✅` and verify the user receives a private one-use invite link.
5. Join the channel with that link and confirm `joined_channel_at` updates.
6. Test `/pending_payments`, `/user <telegram_id>`, `/remove_expired_preview`, and the dashboard filters.
7. Keep `AUTO_REMOVE_EXPIRED=false` until manual preview/removal looks correct.

## Railway deployment

1. Push this repository to GitHub.
2. Create a new Railway project from the repo.
3. Add all required environment variables.
4. Railway will use `railway.json` to run:

```bash
python main.py
```

Run this service as a web service. The FastAPI dashboard and Telegram long-polling bot run in the same process. Do not configure a webhook for the bot.
