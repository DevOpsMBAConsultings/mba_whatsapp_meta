# `mba_whatsapp_meta` Security Hardening & Protocol Parity

*   **Author:** DevOps MBA Consultings
*   **Status:** Proposed
*   **Date:** 2026-11-15

--------------------------------------------------------------------------------

## Overview

This design defines targeted fixes and protocol completions for `mba_whatsapp_meta` (`v19.0.1.3.3`, Odoo 19 Community) informed by a code-level comparison against Odoo Enterprise 19.0's `whatsapp*` suite. The objective is to close critical webhook and credential security gaps, complete Meta Cloud API template sync and sending mechanics, and add operational safeguards (24-hour customer service window tracking, queued dispatch, and country-agnostic phone normalization) while preserving `mba_whatsapp_meta`'s lean Python-only footprint, media-download retry cron, and USD cost/analytics tracking.

## The Problem

`mba_whatsapp_meta` currently has three structural weaknesses that block safe multi-client production use:

1. **Critical Security Exposure:** The public webhook endpoint (`controllers/main.py`) does not verify `X-Hub-Signature-256` and falls back to `Account.search([], limit=1)` when `account_id` is missing, allowing unauthenticated payload spoofing. In addition, `token`, `app_secret`, and `webhook_verify_token` on `whatsapp.account` lack `groups=` restrictions, and `WhatsappTemplateVariable.get_value_for_record()` traverses dot-paths with `getattr` without checking `has_access('read')`.
2. **Incomplete Template & Messaging Protocol:** `action_sync_templates()` does not paginate, ignores `lang_code` and `wa_template_uid`, collapses non-approved statuses into `pending`, and lacks webhook handlers for template status/quality updates. On the sending side, `send_to_phone()` omits `HEADER` (text/media/report PDF) and `BUTTON` parameters, and `WhatsappTemplateVariable` ignores `user_name`, `user_phone`, `free_text`, and `portal_link` line types.
3. **Operational Fragility:** All outbound messages run synchronously (`requests.post(..., timeout=15)`) without a batch queue or retryable/bounced error classification, inbound chats hardcode `base.user_admin` instead of using `notify_user_ids`, phone normalization hardcodes Panama `+507`, and the 24-hour Meta customer service window is not tracked before sending.

## Goals

*   **G1 — Webhook & Secret Security:** Reject any webhook POST without a valid HMAC-SHA256 `X-Hub-Signature-256` header matched against the target `whatsapp.account`, remove the `Account.search([], limit=1)` fallback, restrict secret fields to `mba_whatsapp_meta.group_whatsapp_admin`, and enforce `has_access('read')` / safe-field resolution on template variable paths.
*   **G2 — Template Sync & Send Parity:** Support paginated template sync keyed by `(template_name, lang_code, wa_account_id)` and `wa_template_uid`, handle `message_template_status_update` / `message_template_quality_update` webhooks, evaluate all declared `WhatsappTemplateVariable.line_type` values (`field_name`, `user_name`, `user_phone`, `free_text`, `portal_link`), and send `HEADER` (including `report_id` PDFs) and `BUTTON` parameters.
*   **G3 — Inbound Routing & 24h Window Visibility:** Route inbound Discuss chats using `whatsapp.account.notify_user_ids` (with fallback), support outbound attachments from Discuss, and track the 24-hour customer service window on `discuss.channel`.
*   **G4 — Queued Dispatch & Phone Normalization:** Add an `ir.cron` batch dispatch queue with retryable vs. bounced Meta error code classification, and replace the hardcoded `+507` normalizer with country-agnostic normalization (`phonenumbers` / `phone_validation` when available, with `+507` fallback).
*   **G5 — Preserve Existing Strengths:** Keep `mba_whatsapp_meta` free of OWL JS overrides, retain `ir_cron_retry_whatsapp_media_download`, and retain per-message USD cost and 7-day Meta analytics tracking.

## Non-Goals

*   **NG1 — Meta Embedded Signup (`whatsapp_oauth`):** OAuth-based app onboarding is out of scope; manual WABA ID, Phone Number ID, and permanent token configuration remains the standard setup.
*   **NG2 — Discuss OWL Frontend Overrides:** We will not port Enterprise's `static/src/` Discuss OWL patches (custom `channel_type='whatsapp'` UI or seen/fetched bus ticks).
*   **NG3 — 15-Day Message Autovacuum:** We will not delete `whatsapp.message` records after 15 days; message retention is required for monthly USD cost reporting and media download audit.

## Open Questions

*   **Question 1 (Scope of Stage 3 Bridges):** Should lightweight bridge templates/actions for `sale.order` and `account.move` be included in this design or deferred to separate companion modules (`mba_whatsapp_sale`, `mba_whatsapp_account`) to keep `mba_whatsapp_meta` strictly dependent only on `["base", "mail"]`?
*   **Question 2 (Opt-Out Removal on Inbound Reply):** Should a non-opt-out inbound message from a blocked number automatically remove it from `whatsapp.blocklist` (matching Enterprise's `phone.blacklist` behavior), or require manual unblocking?

## Critical User Journeys (CUJs)

### CUJ 1: Verified Inbound Webhook & Agent Routing
*   **Given**: A `whatsapp.account` configured with `app_secret`, `phone_number_id`, and `notify_user_ids`.
*   **When**: Meta sends a POST to `/whatsapp/webhook` with a valid `X-Hub-Signature-256` header for an incoming customer message.
*   **Then**: The controller verifies the HMAC-SHA256 signature, matches the exact `whatsapp.account` by `phone_number_id`, posts the message into a Discuss chat with `notify_user_ids`, updates the 24h window timestamp on the channel, and downloads any attached media (falling back to the retry cron on transient failure). Forged or unsigned requests return HTTP 403.

### CUJ 2: Full-Component Template Sync & Record Send
*   **Given**: Approved Meta templates with `HEADER` (text, media, or `report_id` PDF), `BODY` variables (`field_name`, `portal_link`, `user_name`, `free_text`), and dynamic `BUTTON` URLs.
*   **When**: An admin runs `action_sync_templates()` and a user sends a template from a record wizard.
*   **Then**: All template pages sync without duplicates across languages, variable paths are checked against the user's `read` access, header/body/button components are transmitted to Meta, and template status/quality changes from Meta webhooks update the local template record automatically.

### CUJ 3: Batch Send via Cron Queue & Error Classification
*   **Given**: Multiple `whatsapp.message` records queued in `outgoing` state (e.g., from a server action or campaign).
*   **When**: `ir.cron` processes the outgoing queue.
*   **Then**: Each message is sent once (guarded against duplicate dispatch), transient Meta errors are retried with bounded attempts, permanent/bounced error codes transition directly to `error`/`bounced`, and `cost_usd` is recorded for sent messages.

## Technical Plan (Architecture)

The design upgrades `mba_whatsapp_meta` in place without adding OWL frontend overrides or external SaaS dependencies, preserving its `["base", "mail"]` dependency footprint and existing media-retry and USD cost-tracking capabilities.

```text
Meta Cloud API
  │
  ├──► POST /whatsapp/webhook (controllers/main.py)
  │      1. Resolve whatsapp.account by account_id or metadata.phone_number_id (no search([], limit=1) fallback)
  │      2. Verify X-Hub-Signature-256 HMAC-SHA256 against account.app_secret (consteq); HTTP 403 on mismatch
  │      3. Dispatch event types:
  │         ├── messages      ──► whatsapp.message (inbound) + discuss.channel (24h window + notify_user_ids) + media download/retry hook
  │         ├── statuses      ──► whatsapp.message state/error_code/error_message update
  │         └── template_*    ──► whatsapp.template status / quality_score / category update
  │
  └──◄ Outbound Dispatch (whatsapp.template / whatsapp.message / discuss.channel)
         ├── Interactive / Single Send ──► immediate send_meta_raw()
         └── Batch / Server Action     ──► whatsapp.message (state='outgoing') ──► ir.cron queue (retryable vs. bounced error codes)
```

### 1. Webhook & Credential Security (`controllers/main.py`, `models/whatsapp_account.py`, `models/whatsapp_template.py`)
*   **HMAC-SHA256 Signature Verification:** `WhatsAppWebhookController.whatsapp_webhook()` will parse `request.httprequest.get_data()` before JSON decoding, resolve the target `whatsapp.account` strictly by explicit `account_id` or `entry[].changes[].value.metadata.phone_number_id` (returning HTTP 403/404 if unmatched instead of `Account.search([], limit=1)`), and compare `X-Hub-Signature-256` (`sha256=<hex>`) against `hmac.new(account.app_secret.encode(), raw_body, hashlib.sha256).hexdigest()` using `odoo.tools.consteq`. If `app_secret` is missing on the account, webhook POSTs are rejected.
*   **Secret Field Isolation:** On `WhatsappAccount`, `token`, `app_secret`, and `webhook_verify_token` will carry `groups="mba_whatsapp_meta.group_whatsapp_admin"` so `base.group_user` and `group_whatsapp_user` cannot read credentials via UI or RPC.
*   **Safe Template Variable Traversal:** `WhatsappTemplateVariable.get_value_for_record(record)` will validate `field_name` against read access (`record.sudo(False).check_access("read")` / `has_access("read")`) and traverse dot-paths using Odoo's permission-checked field path resolution instead of raw `getattr` on `sudo()` records.

### 2. Template Sync, Webhooks & Multi-Component Sending (`models/whatsapp_account.py`, `models/whatsapp_template.py`)
*   **Paginated & Uniquely Keyed Sync:** `WhatsappAccount.action_sync_templates()` will follow Meta `paging.next` cursors until exhausted, store Meta's `id` in `wa_template_uid`, and match/upsert on `(wa_template_uid)` or `(template_name, lang_code, wa_account_id)` with a SQL uniqueness constraint. Statuses will map explicitly (`APPROVED` → `approved`, `PENDING` → `pending`, `REJECTED` → `rejected`, `PAUSED` → `paused`, `DISABLED` → `disabled`), and `quality_score` plus `BUTTONS` components will be synced.
*   **Template Lifecycle Webhooks:** `controllers/main.py` will handle `message_template_status_update`, `message_template_quality_update`, and `template_category_update` events to update `whatsapp.template` state, `quality_score`, and `category` in real time.
*   **Variable Evaluation & Component Payloads:** `WhatsappTemplateVariable.get_value_for_record()` will implement all declared `line_type` options (`field_name`, `user_name`, `user_phone`, `free_text`, `portal_link`). `WhatsappTemplate.send_to_phone()` will build `header` components (text parameter, static media URL/ID, or generated `report_id` PDF uploaded via Meta Media API) and dynamic `button` URL parameters alongside `body` parameters.

### 3. Discuss Routing, 24h Window & Outbound Attachments (`controllers/main.py`, `models/discuss_channel.py`)
*   **Responsible Agent Routing:** Inbound messages will open or reuse a Discuss chat with `account.notify_user_ids` (falling back to `base.user_admin` only if `notify_user_ids` is empty) instead of hardcoding `base.user_admin`.
*   **24-Hour Customer Service Window:** `discuss.channel` will store `whatsapp_last_inbound_dt` and compute `whatsapp_window_valid_until` (`+24h`) and `whatsapp_window_active`. Outbound free-text replies in `DiscussChannel.message_post()` will check `whatsapp_window_active` before calling Meta and post a clear local warning if expired.
*   **Outbound Attachments:** `DiscussChannel.message_post()` will support sending `ir.attachment` files (images, documents, audio) attached to a Discuss message via Meta's Media Upload / send API rather than dropping attachments silently.

### 4. Queued Dispatch, Error Classification & Phone Normalization (`models/whatsapp_message.py`, `models/whatsapp_account.py`, `data/whatsapp_cron.xml`)
*   **Cron Batch Queue & Deduplication:** `WhatsappMessage` will support `outgoing` state for bulk/server-action sends processed by a new `ir.cron` (`ir_cron_send_whatsapp_queue`), committing per batch and guarding against duplicate dispatch of the same `(wa_account_id, mobile_number, template_id, res_model, res_id)` within a send batch.
*   **Error Classification:** Meta error codes will be classified into retryable (rate limits / transient 130429, 131016, 131048, HTTP 5xx) vs. permanent/bounced (131026, 131047, 131051, 132000), transitioning permanent failures directly to `error` or `bounced` without wasting retries.
*   **Country-Agnostic Phone Normalization:** `WhatsappAccount.clean_phone_number()` will use `phonenumbers` (E.164 normalization using `partner.country_id` or `company_id.country_id` when available) and retain the 8-digit `+507` Panama fallback only when no country code or `phonenumbers` match applies.

## Alternatives Considered

### Alternative A: Keep `mba_whatsapp_meta` as-is (No-Op)
*   **Pros**: Zero development effort; preserves existing production behavior for current Panama deployments.
*   **Cons**: Leaves the public `/whatsapp/webhook` endpoint unauthenticated (vulnerable to forged payloads and blocklist poisoning), exposes `token` and `app_secret` to all internal users, fails to paginate or handle non-approved template states, and silently drops header/button template parameters.
*   **Reason for Rejection**: Rejected by author: unauthenticated webhook spoofing and exposed permanent Meta tokens are unacceptable security risks for multi-client production use.

### Alternative B: Port Odoo Enterprise `whatsapp*` Architecture to Community
*   **Pros**: Full parity with Enterprise's Discuss OWL UI (`channel_type='whatsapp'`, seen/fetched ticks, reply-to threading) and `phone.blacklist` integration.
*   **Cons**: High upgrade and maintenance cost across Odoo versions (extensive `static/src/` OWL Discuss overrides and 965-line `whatsapp_template.py`), relies on `whatsapp_oauth` SaaS app infrastructure, introduces a 15-day `whatsapp.message` autovacuum that breaks monthly USD cost/analytics reporting, and carries OEEL-1 licensing restrictions that prevent porting Enterprise code into LGPL-3 Community modules.
*   **Reason for Rejection**: Rejected by author: high maintenance overhead and OEEL-1 licensing constraints; targeted hardening of `mba_whatsapp_meta` achieves protocol and security parity without OWL overrides or losing cost/media-retry features.

### Alternative C: Replace `whatsapp.blocklist` with `phone_validation` (`phone.blacklist`)
*   **Pros**: Reuses Odoo's built-in `phone.blacklist` model and mail blacklist mixin.
*   **Cons**: Adds `phone_validation` as a mandatory manifest dependency and mixes WhatsApp opt-out records with SMS/phone marketing blacklists.
*   **Reason for Rejection**: Rejected by author: keeping `whatsapp.blocklist` preserves the minimal `["base", "mail"]` dependency footprint while keeping WhatsApp opt-outs isolated and auditable.

## Non-Functional Requirements

*   **Security & Privacy**: Webhook POSTs must be authenticated with constant-time HMAC-SHA256 (`consteq`) against `app_secret`; secret fields on `whatsapp.account` must be restricted to `group_whatsapp_admin`; template variable dot-path traversal must enforce the executing user's `read` permissions.
*   **Backward Compatibility**: Existing `whatsapp.account`, `whatsapp.template`, `whatsapp.message`, and `whatsapp.blocklist` records must upgrade cleanly via standard `-u mba_whatsapp_meta` without breaking existing Panama `+507` phone records or losing historical `cost_usd` data.

## Detailed Implementation Plan

All changes apply to `/Users/mbaconsultings/development/19.0/mba_whatsapp_meta/`.

### Target Files to Create/Modify

#### 1. `controllers/main.py` (Modify)
*   **Type / Scope**: HTTP Webhook Controller
*   **Proposed Changes**:
    *   In `WhatsAppWebhookController.whatsapp_webhook()`, read raw request bytes before JSON parsing, resolve `whatsapp.account` strictly by `account_id` or `entry[].changes[].value.metadata.phone_number_id`, and remove the `Account.search([], limit=1)` fallback.
    *   Enforce HMAC-SHA256 `X-Hub-Signature-256` verification against `account.app_secret` using `odoo.tools.consteq`; return HTTP 403 when `app_secret` is unset or the signature fails.
    *   Add webhook event handlers for `message_template_status_update`, `message_template_quality_update`, and `template_category_update` to update `whatsapp.template` records.
    *   Route inbound messages to Discuss chats using `account.notify_user_ids` (falling back to `base.user_admin` only when empty) and update `whatsapp_last_inbound_dt` on `discuss.channel`.

#### 2. `models/whatsapp_account.py` (Modify)
*   **Type / Scope**: Account Configuration, Sync & Phone Normalization Model
*   **Proposed Changes**:
    *   Add `groups="mba_whatsapp_meta.group_whatsapp_admin"` to `token`, `app_secret`, and `webhook_verify_token`.
    *   Update `action_sync_templates()` to follow `paging.next` cursors, store Meta's `id` in `wa_template_uid`, match/upsert by `wa_template_uid` or `(template_name, lang_code, wa_account_id)`, map all Meta statuses (`approved`, `pending`, `rejected`, `paused`, `disabled`), and sync `quality_score` and `BUTTONS`.
    *   Update `clean_phone_number()` to normalize E.164 via `phonenumbers` (using partner/company country when available) while keeping the 8-digit `+507` fallback.
    *   Add helper to upload outbound media files to Meta's `/{phone_number_id}/media` endpoint for Discuss attachments and template PDF headers.

#### 3. `models/whatsapp_template.py` (Modify)
*   **Type / Scope**: Template & Variable Resolution Model
*   **Proposed Changes**:
    *   Add `wa_template_uid`, `quality_score`, and `report_id` (optional `ir.actions.report` for PDF headers) plus a SQL uniqueness constraint on `(template_name, lang_code, wa_account_id)` on `WhatsappTemplate`.
    *   In `WhatsappTemplateVariable.get_value_for_record(record)`, enforce `record.sudo(False).check_access("read")` / field read-access checks instead of raw `getattr` traversal, and implement evaluation for `user_name`, `user_phone`, `free_text`, and `portal_link` alongside `field_name`.
    *   Update `send_to_phone()` to assemble `header` components (text, static media, or rendered `report_id` PDF) and dynamic `button` URL parameters in addition to `body` parameters.

#### 4. `models/whatsapp_message.py` (Modify)
*   **Type / Scope**: Message State, Queue & Error Classification Model
*   **Proposed Changes**:
    *   Add `outgoing` and `bounced` states to `WhatsappMessage.state`, plus `retry_count` and `dedup_key` fields.
    *   Define `RETRYABLE_ERROR_CODES` vs. `BOUNCED_ERROR_CODES` sets for Meta Graph API error responses.
    *   Add `cron_send_outgoing_messages()` to dispatch queued `outgoing` messages in batches with per-batch `cr.commit()`, duplicate-send protection, and bounded retries.

#### 5. `models/discuss_channel.py` (Modify)
*   **Type / Scope**: Discuss Bridge & 24h Window Model
*   **Proposed Changes**:
    *   Add `whatsapp_last_inbound_dt`, `whatsapp_window_valid_until` (`+24h`), and computed `whatsapp_window_active` fields on `discuss.channel`.
    *   In `message_post()`, check `whatsapp_window_active` before dispatching free-text replies, and upload/send `ir.attachment` records attached to outbound Discuss messages via `whatsapp.account`.

#### 6. `data/whatsapp_cron.xml` & `views/whatsapp_template_views.xml` & `views/whatsapp_message_views.xml` (Modify)
*   **Type / Scope**: Cron Job & Backend Views
*   **Proposed Changes**:
    *   Add `ir_cron_send_whatsapp_queue` in `data/whatsapp_cron.xml` calling `whatsapp.message.cron_send_outgoing_messages()`.
    *   Expose `quality_score`, `report_id`, `wa_template_uid`, and `rejected`/`paused`/`disabled` status badges in `views/whatsapp_template_views.xml`, and `outgoing`/`bounced` states in `views/whatsapp_message_views.xml`.

#### 7. `tests/test_whatsapp_meta.py` (Modify)
*   **Type / Scope**: Unit & Integration Tests
*   **Proposed Changes**:
    *   Add test cases covering: (a) HMAC-SHA256 valid/invalid/missing signature rejection and removal of `search([], limit=1)` fallback, (b) `token`/`app_secret` field-group access restriction for non-admin users, (c) `WhatsappTemplateVariable` read-permission check and `user_name`/`user_phone`/`free_text`/`portal_link` resolution, (d) paginated template sync and `HEADER`/`BUTTON` payload construction, (e) 24h window enforcement on `discuss.channel`, and (f) `cron_send_outgoing_messages()` retryable vs. bounced classification.

### Verification Plan

*   **Module Upgrade & Schema Check**: Upgrade `mba_whatsapp_meta` on a test Odoo 19 database (`odoo -u mba_whatsapp_meta --stop-after-init`) and verify zero migration errors on existing `whatsapp.account`, `whatsapp.template`, and `whatsapp.message` rows.
*   **Automated Unit Tests**: Run `odoo-bin --test-tags /mba_whatsapp_meta --stop-after-init` with all new and existing test cases in `tests/test_whatsapp_meta.py` passing.
*   **Security & Access Check**: Verify via RPC / non-admin user session that `whatsapp.account` reads omit `token`, `app_secret`, and `webhook_verify_token`, and unsigned webhook POSTs return HTTP 403.

--------------------------------------------------------------------------------

*Drafted with EGM*
