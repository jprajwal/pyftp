import argparse
import base64
import os
import tomllib
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass
from ftplib import FTP
from typing import TypeAlias


@dataclass(frozen=True)
class FTPConfig:
    name: str
    username: str
    password: str
    host: str
    port: int


class FTPConfigParser(ABC):
    def parse(self) -> list[FTPConfig]:
        raise NotImplementedError()


Choice: TypeAlias = int


class UI(ABC):
    def display_choice_menu(
        self,
        ls: list[str],
        title: str = "",
        prompt: str = "Enter your choice: "
    ) -> Choice:
        raise NotImplementedError()


class CommandLineUI(UI):
    def display_choice_menu(
        self,
        ls: list[str],
        title: str = "",
        prompt: str = "Enter your choice: "
    ) -> Choice:
        print(title)
        for i, item in enumerate(ls, 1):
            print(f"{i}. {item}")
        while True:
            choice = input(prompt)
            try:
                choice_int = int(choice)
                if choice_int > len(ls) or choice_int < 1:
                    print(f"Please enter between 1 and {len(ls)}")
                    continue
                return choice_int - 1
            except Exception:
                continue

        

class PasswordDecoder(ABC):
    @abstractmethod
    def decode(self, password: str) -> str:
        raise NotImplementedError()


class Base64PasswordDecoder(PasswordDecoder):
    def decode(self, password: str) -> str:
        return base64.b64decode(password).decode("utf-8")


class UnknownPasswordDecoder(PasswordDecoder):
    def decode(self, password: str) -> str:
        raise Exception("Unknown password decoder")


class PasswordDecoderFactory:
    @classmethod
    def get_decoder(cls, encoding: str) -> PasswordDecoder:
        match encoding:
            case "base64":
                return Base64PasswordDecoder()
            case _:
                return UnknownPasswordDecoder()


class TomlFTPConfigParser(FTPConfigParser):
    def __init__(self, filename: str = "ftpconfig.toml") -> None:
        self._fname = filename

    def parse(self) -> list[FTPConfig]:
        with open(self._fname, "rb") as fd:
            data = tomllib.load(fd)
        configs = []
        for server in data["server"]:
            configs.append(
                FTPConfig(
                    username=server["user"],
                    name=server["name"],
                    password=server["password"],
                    host=server["host"],
                    port=int(server["port"]),
                )
            )
        return configs


class FileZillaFTPConfigParser(FTPConfigParser):
    def __init__(self, filename: str = "FileZilla.xml") -> None:
        self._fname = filename

    def _decode_password(self, encoding: str, password: str) -> str:
        decoder = PasswordDecoderFactory.get_decoder(encoding)
        return decoder.decode(password=password)

    def parse(self) -> list[FTPConfig]:
        tree = ET.parse(self._fname)
        root = tree.getroot()
        servers: list[FTPConfig] = list()

        def get_value(element: ET.Element | None) -> str:
            return "" if element is None else (element.text or "")

        for server in root.iter('Server'):
            name = get_value(server.find("Name"))
            username = get_value(server.find("User"))
            host = get_value(server.find("Host"))
            port = get_value(server.find("Port"))
            password_node = server.find("Pass")
            if (password_node := server.find("Pass")) is None:
                password = ""
            else:
                password = self._decode_password(
                    encoding=password_node.attrib["encoding"],
                    password=password_node.text or "",
                )
            servers.append(
                FTPConfig(
                    name=name,
                    username=username,
                    password=password,
                    host=host,
                    port=int(port),
                )
            )
        return servers

def ftp_ls(ftp: FTP, args: argparse.Namespace):
    files = ftp.nlst(" ".join(args.path))
    print(files)


def ftp_download(ftp: FTP, args: argparse.Namespace):
    filename = args.path.split("/")[-1]
    with open(filename, "wb") as fd:
        ftp.retrbinary(f"RETR {args.path}", fd.write)
    print(f"{os.getcwd()}/{filename}")


def _upload(ftp: FTP, f: str, dest: str):
    with open(f, "rb") as fd:
        ftp.cwd(dest)
        ftp.storbinary(f"STOR {os.path.basename(f)}", fd)


def ftp_recursive_upload(ftp, f: str, dest: str):
    if not os.path.isdir(f):
        raise Exception("ftp_recursive_upload() must be used only for dirs")
    dirs = [(f, dest), ]
    while len(dirs) > 0:
        src_dir, dest_dir = dirs.pop(0)
        ftp.cwd(dest_dir)
        basename = os.path.basename(src_dir)
        ftp.mkd(basename)
        dest_dir = os.path.join(dest_dir, basename)
        items = os.listdir(src_dir)
        for item in items:
            filename = os.path.join(src_dir, item)
            if os.path.isdir(filename):
                dirs.append((
                    filename,
                    dest_dir,
                ))
                continue
            _upload(ftp, filename, dest_dir)


def ftp_upload(ftp: FTP, args: argparse.Namespace):
    for f in args.src:
        if os.path.isdir(f):
            ftp_recursive_upload(ftp, f, args.dest)
            continue
        _upload(ftp, f, args.dest)


parser = argparse.ArgumentParser(description="ftp client")
parser.set_defaults(func=lambda *x: parser.print_help())
sub_parsers = parser.add_subparsers(description="FTP commands")
ls = sub_parsers.add_parser("ls", help="list files/directories in specified file/directory")
ls.add_argument("path", nargs='*', default="/")
ls.set_defaults(func=ftp_ls)
download = sub_parsers.add_parser("download", help="download files/directories")
download.add_argument("path", nargs='?')
download.set_defaults(func=ftp_download)
upload = sub_parsers.add_parser("upload", help="upload files/directories")
upload.add_argument("src", nargs='+', help="source file/directory")
upload.add_argument("dest", help="name of the destination directory where the files/directories must be uploaded")
upload.set_defaults(func=ftp_upload)

args = parser.parse_args()
ftpconfigs = TomlFTPConfigParser("test_ftpconfig.toml").parse()
ui = CommandLineUI()
choice = ui.display_choice_menu(
    ls=list(map(lambda x: f"{x.name} ({x.host})", ftpconfigs)),
    title="FTP Servers:",
    prompt="Please choose FTP server: "
)
ftpconfig = ftpconfigs[choice]
ftp = FTP(ftpconfig.host)
ftp.login(user=ftpconfig.username, passwd=ftpconfig.password)
args.func(ftp, args)
ftp.quit()
