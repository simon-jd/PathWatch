#!/usr/bin/env python3
"""Install pinned development binaries into the project; never modifies HOME."""
import hashlib
import io
import pathlib
import argparse
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".tools"
VERSIONS = {"terraform": "1.17.0", "tflint": "0.65.0", "go": "1.27.3", "rust": "1.99.1"}


def fetch(url):
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def checked(url, checksum_url, filename):
    data = fetch(url)
    checksums = fetch(checksum_url).decode()
    expected = next(line.split()[0] for line in checksums.splitlines()
                    if line.split()[-1].lstrip("*") == filename)
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise RuntimeError(f"SHA256 mismatch: {filename}")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rust", action="store_true", help="also install the Rust toolchain")
    args = parser.parse_args()
    (TOOLS / "bin").mkdir(parents=True, exist_ok=True)
    for name in ["terraform", "tflint"]:
        target = TOOLS / "bin" / name
        if target.exists():
            continue
        version = VERSIONS[name]
        filename = f"{name}_{version}_linux_amd64.zip" if name == "terraform" else "tflint_linux_amd64.zip"
        base = (f"https://releases.hashicorp.com/terraform/{version}" if name == "terraform"
                else f"https://github.com/terraform-linters/tflint/releases/download/v{version}")
        checksums = f"terraform_{version}_SHA256SUMS" if name == "terraform" else "checksums.txt"
        data = checked(f"{base}/{filename}", f"{base}/{checksums}", filename)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            target.write_bytes(archive.read(name))
        target.chmod(0o755)
        print(f"installed {name} {version}", flush=True)
    if not (TOOLS / "go/bin/go").exists():
        version = VERSIONS["go"]
        filename = f"go{version}.linux-amd64.tar.gz"
        data = fetch(f"https://go.dev/dl/{filename}")
        # Pinned from https://go.dev/dl/?mode=json at project setup.
        expected = "65efbc80fdc1fa2cd2631cad5b481777a68e8725f34e89f50fc6ac714e379b40"
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError("Go SHA256 mismatch")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            archive.extractall(TOOLS, filter="data")
        print(f"installed go {version}", flush=True)
    if args.rust and not (TOOLS / "rust/bin/rustc").exists():
        version = VERSIONS["rust"]
        package = f"rust-{version}-x86_64-unknown-linux-gnu"
        data = fetch(f"https://static.rust-lang.org/dist/2026-10-15/{package}.tar.xz")
        # Pinned from the official stable channel manifest on 2026-10-23.
        expected = "231fcae47ee950621374725b5fc3b5441d2fbda2955ac45dd7dc816d8876cfe7"
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError("Rust SHA256 mismatch")
        with tempfile.TemporaryDirectory(prefix="rust-install-", dir=TOOLS) as directory:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as archive:
                archive.extractall(directory, filter="data")
            subprocess.run(["bash", str(pathlib.Path(directory) / package / "install.sh"),
                            f"--prefix={TOOLS / 'rust'}", "--disable-ldconfig",
                            "--components=rustc,rust-std-x86_64-unknown-linux-gnu,cargo,rustfmt-preview,clippy-preview,llvm-tools-preview"], check=True)
        print(f"installed rust {version}", flush=True)


if __name__ == "__main__":
    main()
