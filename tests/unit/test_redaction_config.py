from pathlib import Path

from memory_net.shared.redaction import Redactor

CONFIG = Path("config/redaction_patterns.yaml")


# Every sample is fake, but several are shaped exactly like the real credential
# they stand for, which is the whole point: the redactor has to match that shape.
# The `"prefix" + "body"` splits below are deliberate — a secret scanner reads the
# source, not the concatenated value, so keeping the halves apart is what lets a
# push through. NEVER merge a split pair back into one contiguous literal, and do
# not let a formatter or a "simplification" do it either; black only rewraps them.
SAMPLES = {
    "email": "contact me at alice@example.com",
    "phone_cn": "call 13800138000 tomorrow",
    "id_card_cn": "id 11010519491231002X",
    "api_key_prefix": "key sk_abcdefghijklmnopqrstuvwxyz",
    "bearer_token": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
    "api_key_generic": "api_key=abcdefghijklmnopqrstuvwxyz123456",
    "aws_access_key": "AKIAIOSFODNN7EXAMPLE",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    "slack_webhook": "https://hooks.slack.com/services/"
    + "T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX",
    "discord_webhook": "https://discord.com/api/webhooks/"
    + "123456789012345678/abcdefghijklmnopqrstuvwxyz",
    "db_url_with_password": "postgres://user:pass@example.internal/db",
    "credit_card": "4111 1111 1111 1111",
    "github_token": "ghp_" + "abcdefghijklmnopqrstuvwxyzABCDEFGHIJ1234",
    "gcp_service_account_key": '"private_key":"-----BEGIN '
    + 'PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----"',
    "anthropic_api_key": "sk-ant-api03-" + "a" * 80,
    "openai_api_key": "sk-proj-" + "a" * 24 + "T3BlbkFJ" + "b" * 24,
    "stripe_secret_key": "sk_live_" + "abcdefghijklmnopqrstuvwxyz123456",
    "npm_token": "npm_" + "abcdefghijklmnopqrstuvwxyzABCDEFGHIJ",
    "google_api_key": "AIza" + "a" * 35,
    "azure_storage_connection_string": "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey="
    + "a" * 44
    + ";EndpointSuffix=core.windows.net",
    "sentry_dsn": "https://abc123@o123456.ingest." + "sentry.io/7890123",
    "ipv4_private": "service at 192.168.1.10",
    "ssh_url_with_user": "git@example.com:org/private.git",
}


def test_default_redaction_patterns_cover_target_secret_types() -> None:
    redactor = Redactor.from_yaml(CONFIG)

    for rule_name, sample in SAMPLES.items():
        result = redactor.redact(sample)
        assert result.stats.get(rule_name) == 1, rule_name
        if not result.blocked:
            assert sample not in result.content


def test_default_redaction_patterns_block_private_keys() -> None:
    redactor = Redactor.from_yaml(CONFIG)
    result = redactor.redact(
        "-----BEGIN " + "PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----"
    )

    assert result.blocked is True
    assert "private_key" in result.blocked_rules
