"""Save and load the local server’s Earthdata credential."""
import getpass
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
import warnings

TOKEN_FILE = Path(__file__).resolve().parents[1] / ".earthdata-token"
BEARER = re.compile(r"[A-Za-z0-9._~+/=-]+")


def validate_token(token):
    if not BEARER.fullmatch(token):
        raise ValueError("Paste the bearer token from Earthdata without spaces or quotes.")
    return token


def save_token(path, token):
    token = validate_token(token)
    with NamedTemporaryFile(mode="w", dir=path.parent, prefix=".earthdata-token-", delete=False) as file:
        temp = Path(file.name)
        try:
            os.fchmod(file.fileno(), 0o600)
            file.write(token + "\n")
            file.close()
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def load_token(path=TOKEN_FILE):
    if os.environ.get("EARTHDATA_TOKEN", "").strip() or not path.exists():
        return
    if path.stat().st_mode & 0o077:
        raise ValueError("Restrict access to .earthdata-token with chmod 600 .earthdata-token.")
    os.environ["EARTHDATA_TOKEN"] = validate_token(path.read_text().strip())


def main():
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            token = getpass.getpass("Paste your NASA Earthdata bearer token: ").strip()
            save_token(TOKEN_FILE, token)
        except (OSError, EOFError, ValueError, getpass.GetPassWarning) as error:
            raise SystemExit(f"Could not save the Earthdata token. {error}") from None
    print("Earthdata token saved. Restart CryoTrace to use it.")


if __name__ == "__main__":
    main()
