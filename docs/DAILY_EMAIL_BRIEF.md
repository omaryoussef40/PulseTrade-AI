# Daily email brief — hello@orora.me

Open **Daily Stock Scanner → Daily Brief**. The view is prepared for the
Microsoft 365 mailbox `hello@orora.me`; the domain's public MX record points to
`orora-me.mail.protection.outlook.com`. It is not connected until Microsoft
app setup and sign-in are completed.

## One-time connection

1. In [Microsoft Entra app registrations](https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade), register **PulseTrade Daily Brief** in your organization's directory.
2. Add Microsoft Graph **delegated** permissions `Mail.Read` and `Mail.Read.Shared`. The latter supports a shared mailbox when the signed-in user already has access. Your organization may require administrator consent.
3. Under Authentication → Advanced settings, enable **Allow public client flows**.
4. Copy the **Application (client) ID** into Daily Brief, leave the tenant as `orora.me` (or supply your directory tenant ID), and save the connection settings. No client secret is used.
5. Press **Connect Microsoft mailbox**, open the Microsoft sign-in link, enter the displayed code and approve read access. Press **I have signed in — check connection** afterward.
6. Press **Check inbox now**. Set **Sender contains** and **Subject contains** to match the daily email. The default sender filter is `openai`; the search reads the latest matching message from Inbox within the configured lookback period.

For a shared mailbox, sign in with the Microsoft user who has delegated access
to `hello@orora.me`. A personal Gmail connection in Codex does not grant the
local application access to this Microsoft mailbox.

Microsoft references: [app registration](https://learn.microsoft.com/en-us/graph/auth-register-app-v2),
[device sign-in](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code),
[shared mailbox access](https://learn.microsoft.com/en-us/graph/outlook-share-messages-folders).

## Daily use

While Daily Brief is open, it checks every five minutes when automatic checks
are enabled. It displays the matching email subject, sender, received time in
Dubai, and the brief as plain text. The manual check button can refresh sooner.
The current implementation reads **Inbox**, so mail routed into another folder
must be moved to Inbox or have its mail rule adjusted.

Supported Pine code can be in a `.pine` attachment, a fenced code block in the
body, or the body itself. Prices, signal rules and the plan date go through the
existing Pine parser. Press **Load this Pine into the scanner for review**, then
open Charts or Paste & settings. Saving and enabling a trading plan still use
the existing controls. Incoming email never authorizes broker orders.

If ChatGPT sends only a notification linking to the result, the app displays
that link. The email connection cannot extract code that is absent from the
email; open the linked result and paste its Pine code.

Sign-in tokens are stored through the system credential store (macOS Keychain
on this Mac), not in config.json, exports or source code. The selected brief is
saved locally with owner-only file permissions in `data/daily_email_brief.json`.
The connection cannot send, delete or mark messages read. Disconnecting clears
the stored sign-in and saved brief. No Microsoft account was accessed while
implementing this feature; authentication and real-message delivery remain to
be verified after setup.
