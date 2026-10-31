"""Redact configured credentials and sensitive URL parameters from application logs."""
import logging
import os
import re


def redact(text, values):
    text = str(text)
    for value in values:
        text = text.replace(value, '[REDACTED]')
    text = re.sub(r'(/bot)\d+:[A-Za-z0-9_-]+', r'\1[REDACTED]', text)
    text = re.sub(
        r'(authorization\s*[:=]\s*[\"\']?)(?:Basic|Bearer)\s+[^\s\"\',}]+',
        r'\1[REDACTED]', text, flags=re.IGNORECASE,
    )
    text = re.sub(
        r'([\"\']?(?:access_token|refresh_token|id_token|client_secret)[\"\']?\s*[:=]\s*[\"\']?)[^\s\"\',}]+',
        r'\1[REDACTED]', text, flags=re.IGNORECASE,
    )
    return re.sub(
        r'([?&](?:code|state|access_token|refresh_token|id_token)=)[^&\s\"\'<>]+',
        r'\1[REDACTED]', text, flags=re.IGNORECASE,
    )


class RedactingFormatter(logging.Formatter):
    def __init__(self, wrapped, values):
        super().__init__()
        self.wrapped = wrapped or logging.Formatter()
        self.values = values

    def format(self, record):
        return redact(self.wrapped.format(record), self.values)


def install_security_logging():
    values = sorted({value for name, value in os.environ.items()
                     if len(value) >= 6 and any(part in name.upper()
                     for part in ('TOKEN', 'SECRET', 'PASSWORD', 'ADMIN_PASS', 'API_KEY'))},
                    key=len, reverse=True)
    handlers = set(logging.getLogger().handlers)
    for logger in logging.Logger.manager.loggerDict.values():
        if isinstance(logger, logging.Logger):
            handlers.update(logger.handlers)
    for handler in handlers:
        if not isinstance(handler.formatter, RedactingFormatter):
            handler.setFormatter(RedactingFormatter(handler.formatter, values))
    # HTTP client info logs include full URLs, including Telegram bot tokens.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
