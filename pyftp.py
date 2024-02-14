import argparse
import base64
import os
import sys
import tomllib
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass
from ftplib import FTP
from typing import Generator, TypeAlias

from prompt_toolkit import HTML, print_formatted_text, prompt
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.completion.base import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText


class FTPPathCompleter(Completer):
    def __init__(self, ftp: FTP) -> None:
        self._ftp = ftp

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Generator[Completion, None, None]:
        path = document.text
        basename = os.path.basename(path)
        dirname = os.path.dirname(path)
        self._ftp.cwd(dirname)
        ls = filter(lambda f: f.startswith(basename), self._ftp.nlst())
        for f in ls:
            yield Completion(f, start_position=len(basename) * -1)


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
    @abstractmethod
    def display_choice_menu(
        self,
        ls: list[str],
        title: str = "",
        prompt_str: str = "Enter your choice: ",
    ) -> Choice:
        ...

    @abstractmethod
    def prompt_user(self, prompt_str: str) -> str:
        ...

    @abstractmethod
    def print_error(self, msg: str) -> None:
        ...

    @abstractmethod
    def print_msg(self, msg: str, color: str = "") -> None:
        ...


class CommandLineUI(ABC):
    def display_choice_menu(
        self,
        ls: list[str],
        title: str = "",
        prompt_str: str = "Enter your choice: ",
    ) -> Choice:
        self.print_msg(title, color="blue")
        for i, item in enumerate(ls, 1):
            self.print_msg(f"{i}. {item}", color="blue")
        while True:
            choice = self.prompt_user(prompt_str)
            try:
                choice_int = int(choice)
                if choice_int > len(ls) or choice_int < 1:
                    self.print_error(f"Please enter between 1 and {len(ls)}")
                    continue
                return choice_int - 1
            except Exception:
                continue

    def prompt_user(self, prompt_str: str) -> str:
        return input(prompt_str)

    def print_error(self, msg: str) -> None:
        print(msg, file=sys.stderr)

    def print_msg(self, msg: str, color: str = "") -> None:
        print(msg)


class PromptToolkitUI(UI):
    def display_choice_menu(
        self,
        ls: list[str],
        title: str = "",
        prompt_str: str = "Enter your choice: ",
    ) -> Choice:
        self.print_msg(title, color="blue")
        for i, item in enumerate(ls, 1):
            self.print_msg(f"{i}. {item}", color="blue")
        while True:
            choice = self.prompt_user(prompt_str)
            try:
                choice_int = int(choice)
                if choice_int > len(ls) or choice_int < 1:
                    self.print_error(f"Please enter between 1 and {len(ls)}")
                    continue
                return choice_int - 1
            except Exception:
                continue

    def prompt_user(self, prompt_str: str) -> str:
        return prompt(prompt_str)

    def print_error(self, msg: str) -> None:
        print_formatted_text(HTML(f"<ansired>{msg}</ansired>"))

    def print_msg(self, msg: str, color: str = "") -> None:
        print_formatted_text(
            FormattedText(
                [
                    (color or "#FFFFFF", msg),
                ]
            )
        )


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

        for server in root.iter("Server"):
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


def ftp_ls(ftp: FTP, args: argparse.Namespace) -> None:
    files = ftp.nlst(" ".join(args.path))
    print(files)


def _is_file(ftp: FTP, f: str) -> bool:
    try:
        ftp.size(f)
        return True
    except Exception:
        return False


def ftp_download(ftp: FTP, args: argparse.Namespace) -> None:
    dirname = os.path.dirname(args.ftp_path)
    filename = os.path.basename(args.ftp_path)
    if _is_file(ftp, args.ftp_path):
        with open(filename, "wb") as fd:
            ftp.retrbinary(f"RETR {args.ftp_path}", fd.write)
        print(f"{os.getcwd()}/{filename}")
        return
    # maybe a directory
    ftp.cwd(dirname)
    ls = ftp.nlst()
    if filename not in ls:
        raise Exception(f"No such file/dir in FTP path: {dirname}")
    dest_dir = os.path.abspath(args.local_path)
    dirs = [
        (dest_dir, args.ftp_path),
    ]
    if not os.path.exists(dest_dir):
        raise Exception(f"No such file/dir in local fs: {dest_dir}")
    while len(dirs) > 0:
        dest_dir, ftp_d = dirs.pop(0)
        filename = os.path.basename(ftp_d)
        dest_dir = os.path.join(dest_dir, filename)
        os.mkdir(dest_dir)
        ftp.cwd(ftp_d)
        for f in ftp.nlst():
            filename = os.path.join(ftp.pwd(), f)
            if not _is_file(ftp, filename):
                dirs.append((dest_dir, filename))
                continue
            with open(os.path.join(dest_dir, f), "wb") as fd:
                ftp.retrbinary(f"RETR {f}", fd.write)


def _upload(ftp: FTP, f: str, dest: str) -> None:
    with open(f, "rb") as fd:
        ftp.cwd(dest)
        ftp.storbinary(f"STOR {os.path.basename(f)}", fd)


def ftp_recursive_upload(ftp: FTP, f: str, dest: str) -> None:
    if not os.path.isdir(f):
        raise Exception("ftp_recursive_upload() must be used only for dirs")
    dirs = [
        (f, dest),
    ]
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
                dirs.append(
                    (
                        filename,
                        dest_dir,
                    )
                )
                continue
            _upload(ftp, filename, dest_dir)


def ftp_upload(ftp: FTP, args: argparse.Namespace) -> None:
    for f in args.src:
        if os.path.isdir(f):
            ftp_recursive_upload(ftp, f, args.dest)
            continue
        _upload(ftp, f, args.dest)


def main() -> None:
    parser = argparse.ArgumentParser(description="ftp client")
    parser.set_defaults(func=lambda *x: parser.print_help())
    sub_parsers = parser.add_subparsers(description="FTP commands")
    ls = sub_parsers.add_parser(
        "ls", help="list files/directories in specified file/directory"
    )
    ls.add_argument("path", nargs="*", default="/")
    ls.set_defaults(func=ftp_ls)
    download = sub_parsers.add_parser(
        "download", help="download files/directories"
    )
    download.add_argument("ftp_path")
    download.add_argument(
        "local_path",
        default=os.getcwd(),
        help=(
            "local dir where the specified ftp file must be downloaded. "
            + "Default is current working dir"
        ),
    )
    download.set_defaults(func=ftp_download)
    upload = sub_parsers.add_parser("upload", help="upload files/directories")
    upload.add_argument("src", nargs="+", help="source file/directory")
    upload.add_argument(
        "dest",
        help=(
            "name of the destination directory where the files/directories "
            + "must be uploaded"
        ),
    )
    upload.set_defaults(func=ftp_upload)
    args = parser.parse_args()
    ftpconfigs = TomlFTPConfigParser("test_ftpconfig.toml").parse()
    ui = CommandLineUI()
    choice = ui.display_choice_menu(
        ls=list(map(lambda x: f"{x.name} ({x.host})", ftpconfigs)),
        title="FTP Servers:",
        prompt_str="Please choose FTP server: ",
    )
    ftpconfig = ftpconfigs[choice]
    ftp = FTP(ftpconfig.host)
    ftp.login(user=ftpconfig.username, passwd=ftpconfig.password)
    args.func(ftp, args)
    ftp.quit()


def test() -> None:
    ftpconfigs = TomlFTPConfigParser("test_ftpconfig.toml").parse()
    ui = CommandLineUI()
    choice = ui.display_choice_menu(
        ls=list(map(lambda x: f"{x.name} ({x.host})", ftpconfigs)),
        title="FTP Servers:",
        prompt_str="Please choose FTP server: ",
    )
    ftpconfig = ftpconfigs[choice]
    ftp = FTP(ftpconfig.host)
    ftp.login(user=ftpconfig.username, passwd=ftpconfig.password)
    completer = FTPPathCompleter(ftp)
    print(prompt("ftp path: ", completer=completer))


if __name__ == "__main__":
    test()
