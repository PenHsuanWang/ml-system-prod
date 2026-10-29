"""Create private local credentials without displaying or overwriting them."""

import os
from pathlib import Path
import secrets


def main() -> None:
    values = {name: secrets.token_hex(24) for name in (
        "ML_DB_PASSWORD", "MLFLOW_DB_PASSWORD", "ML_MQ_PASSWORD", "ML_MONGO_PASSWORD", "ML_S3_PASSWORD")}
    values.update(APP_JWT_SECRET=secrets.token_hex(
        32), ML_UID=str(os.getuid()), ML_GID=str(os.getgid()))
    descriptor = os.open(".env.ml", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write("\n".join(f"{key}={value}" for key, value in values.items())+"\n")
    Path("var").mkdir(mode=0o700, exist_ok=True)
    print("Created private .env.ml and local var directory")


if __name__ == "__main__":
    main()
