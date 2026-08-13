"""Canonical-user identity linking: /link-email, /unlink-email, /my-accounts, OTP verification."""

import re

from storage import StudentPilotStore

LINK_COMMANDS = ("/link-email", "/link_email")
UNLINK_COMMANDS = ("/unlink-email", "/unlink_email")
ACCOUNTS_COMMANDS = ("/my-accounts", "/my_accounts", "/accounts")


class IdentityService:
    def __init__(self, store: StudentPilotStore, email_sender=None) -> None:
        self._store = store
        self._email_sender = email_sender

    def handle(self, conversation_id: str, user_id: str, text: str) -> str | None:
        stripped = text.strip()
        if stripped in LINK_COMMANDS:
            return self._prompt_link_email()
        if stripped in ACCOUNTS_COMMANDS:
            return self._accounts(user_id)
        for command in UNLINK_COMMANDS:
            if stripped == command:
                return "Which email would you like to unlink? Example: /unlink-email college@example.com"
            if stripped.startswith(command + " "):
                return self._unlink_email(user_id, stripped[len(command):].strip())
        lowered = text.lower().strip()
        if self._looks_like_email(lowered) and "otp" not in lowered and "code" not in lowered:
            return self._start_link_flow(user_id, lowered)
        if self._looks_like_otp(stripped):
            return self._verify_code(user_id, stripped)
        return None

    def _prompt_link_email(self) -> str:
        return ("Please provide the email address you want to link.\n"
                "I'll send a verification code to that address.")

    def _start_link_flow(self, canonical_user_id: str, email: str) -> str:
        if self._store.email_linked_to_user(canonical_user_id, email):
            return f"{email} is already linked to your StudentPilot account."
        code = self._store.create_otp(canonical_user_id, email)
        if not self._send_verification_email(email, code):
            return "I couldn't send the verification email right now. Please try again shortly."
        return (f"I've sent a verification code to {email}.\n"
                "Please enter the code here to verify your account.")

    def _verify_code(self, canonical_user_id: str, code: str) -> str:
        pending = self._store.pending_otp_email(canonical_user_id)
        if not pending:
            return "There's no email verification in progress. Use /link-email to start linking an email."
        success, message = self._store.verify_otp(canonical_user_id, pending, code)
        if success:
            return ("✅ Email successfully linked.\n"
                    "Your Telegram and email conversations can now access the same StudentPilot memory.")
        return message

    def _unlink_email(self, canonical_user_id: str, email: str) -> str:
        if not self._store.email_linked_to_user(canonical_user_id, email):
            return f"{email} is not linked to your StudentPilot account."
        self._store.unlink_identity("email", email)
        return (f"✅ Email address {email} unlinked.\n"
                "Your opportunities, tasks, and reminders are preserved.")

    def _accounts(self, canonical_user_id: str) -> str:
        identities = self._store.identities_for_user(canonical_user_id)
        telegram = [item for item in identities if item["channel"] == "telegram"]
        emails = [item["address"] for item in identities if item["channel"] == "email"]
        lines = ["🔗 Linked accounts"]
        lines.append("Telegram: Connected" if telegram else "Telegram: Not connected")
        if emails:
            lines.append("Email:")
            lines.extend(f"  • {email}" for email in emails)
        else:
            lines.append("Email: None linked")
        return "\n".join(lines)

    @staticmethod
    def _looks_like_email(text: str) -> bool:
        stripped = text.strip().strip(".")
        return bool(re.fullmatch(r"[\w.+-]+@[\w-]+\.[\w.]+", stripped))

    @staticmethod
    def _looks_like_otp(text: str) -> bool:
        return bool(re.fullmatch(r"\d{6}", text.strip()))

    def _send_verification_email(self, email: str, code: str) -> bool:
        if self._email_sender is None:
            return False
        subject = "StudentPilot Email Verification"
        body = f"Your StudentPilot verification code is {code}.\nThis code expires in 10 minutes."
        return self._email_sender(email, subject, body) is not None