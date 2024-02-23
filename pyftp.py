import argparse
import base64
import json
import logging
import os
import sys
import tomllib
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from ftplib import FTP
from typing import Generator, TypeAlias, TypedDict, cast

from prompt_toolkit import HTML, print_formatted_text, prompt
from prompt_toolkit.completion import Completer as PTKCompleter
from prompt_toolkit.completion import Completion as PTKCompletion
from prompt_toolkit.completion import PathCompleter as PTKPathCompleter
from prompt_toolkit.completion.base import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText


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


class Completion:
    def __init__(self, text: str, start_position: int = 0) -> None:
        self._text = text
        self._position = start_position

    def text(self) -> str:
        return self._text

    def start_position(self) -> int:
        return self._position


class Completer(ABC):
    @abstractmethod
    def get_completions(self, inp: str) -> list[Completion]:
        ...


class FTPPathCompleter(Completer):
    COMPLETION_PLACEHOLDER = "..."

    def __init__(self, ftp: FTP) -> None:
        self._ftp = ftp
        self._ftp_cache: dict[str, list[str]] = dict()
        self._ftp_reqs: dict[str, Future[list[str]]] = dict()
        self._pool = ThreadPoolExecutor(max_workers=1)

    def _get_dir_listing(self, dirname: str) -> list[str]:
        logging.debug(f"start:\n{self._ftp_cache=}\n{self._ftp_reqs=}")

        def get_files(*_, **__) -> list[str]:
            try:
                ftp = self._ftp
                ftp.cwd(dirname)
                ls = ftp.nlst()
                logging.debug(f"get_files: {ls}")
                return ls
            except Exception as exc:
                logging.error(str(exc))
                raise

        if dirname not in self._ftp_reqs:
            self._ftp_reqs[dirname] = self._pool.submit(get_files)
        executor = self._ftp_reqs[dirname]
        ls = self._ftp_cache.setdefault(dirname, [])
        if not executor.done():
            logging.debug(f"executing:\n{self._ftp_cache=}\n{self._ftp_reqs=}")
            return ls
        self._ftp_reqs.pop(dirname)
        if executor.exception() is not None:
            ls = self._ftp_cache.pop(dirname)
            return ls
        if (result := executor.result()) is not None:
            self._ftp_cache[dirname] = result
        logging.debug(f"end:\n{self._ftp_cache=}\n{self._ftp_reqs=}")
        return self._ftp_cache[dirname]

    def _remove_placeholder(self, fname: str) -> str:
        return fname.strip(self.COMPLETION_PLACEHOLDER)

    def _get_completions_starting_with(
        self, word: str, ls: list[str]
    ) -> list[str]:
        return list(filter(lambda f: f.startswith(word), ls))

    def _get_completion_replace_length(self, path: str) -> int:
        return len(os.path.basename(path))

    def _path_has_placeholder(self, path: str) -> bool:
        return path.endswith(self.COMPLETION_PLACEHOLDER)

    def get_completions(self, inp: str) -> list[Completion]:
        path = inp
        dirname = os.path.dirname(path)
        basename = self._remove_placeholder(os.path.basename(path))
        ls = self._get_dir_listing(dirname)
        if not ls and self._path_has_placeholder(path):
            return []
        elif not ls:
            return [
                Completion(self.COMPLETION_PLACEHOLDER, 0),
            ]
        else:
            to_return = []
            for f in self._get_completions_starting_with(basename, ls):
                length = self._get_completion_replace_length(path)
                to_return.append(Completion(f, length * -1))
            return to_return


class PathCompleter(Completer):
    def get_completions(self, inp) -> list[Completion]:
        document = Document(inp, len(inp))
        event = CompleteEvent(text_inserted=False, completion_requested=True)
        completer = PTKPathCompleter()
        completions = []
        for c in completer.get_completions(document, event):
            completions.append(Completion(c.text, c.start_position))
        return completions


class CompleterAdaptor(PTKCompleter):
    def __init__(self, completer: Completer) -> None:
        self._completer = completer

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Generator[PTKCompletion, None, None]:
        completions = self._completer.get_completions(document.text)
        for c in completions:
            yield PTKCompletion(
                c.text(),
                start_position=c.start_position(),
            )


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
    def prompt_user(
        self, prompt_str: str, completer: Completer | None = None
    ) -> str:
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

    def prompt_user(
        self, prompt_str: str, completer: Completer | None = None
    ) -> str:
        completer_adapter = (
            CompleterAdaptor(completer) if completer is not None else None
        )
        return prompt(
            prompt_str, completer=completer_adapter, complete_while_typing=False
        )

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


class FTPClient(AbstractContextManager):
    def __init__(self, ftpconfig: FTPConfig | None) -> None:
        self._ftpconfig = ftpconfig
        self._ftp: FTP | None = None

    def __enter__(self) -> FTP:
        if self._ftpconfig is None:
            raise Exception("FTPClient initialized with null config")
        ftp = FTP(self._ftpconfig.host)
        ftp.login(
            user=self._ftpconfig.username, passwd=self._ftpconfig.password
        )
        self._ftp = ftp
        return ftp

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        logging.debug("called __exit__()")
        if self._ftp is not None:
            self._ftp.quit()
        self._ftp = None


def ftp_ls(args: argparse.Namespace) -> None:
    with FTPClient(get_selected_ftp_config()) as ftp:
        parser = argparse.ArgumentParser()
        parser.add_argument("path", nargs="*", default="/")
        inp = args.ui.prompt_user("path: ", completer=FTPPathCompleter(ftp))
        ls_args = parser.parse_args(inp.split())
        files = ftp.nlst(" ".join(ls_args.path))
        print(files)


def _is_file(ftp: FTP, f: str) -> bool:
    try:
        ftp.size(f)
        return True
    except Exception:
        return False


def ftp_download(args: argparse.Namespace) -> None:
    with FTPClient(get_selected_ftp_config()) as ftp:
        ftp_path_parser = argparse.ArgumentParser()
        ftp_path_parser.add_argument("ftp_path")
        inp = args.ui.prompt_user("ftp path: ", completer=FTPPathCompleter(ftp))
        ftp_path_args = ftp_path_parser.parse_args(inp.split())
        local_path_parser = argparse.ArgumentParser()
        local_path_parser.add_argument(
            "local_path",
            default=os.getcwd(),
            help=(
                "local dir where the specified ftp file must be downloaded. "
                + "Default is current working dir"
            ),
        )
        inp = args.ui.prompt_user("local path: ", completer=PathCompleter())
        local_path_args = local_path_parser.parse_args(inp.split())
        dirname = os.path.dirname(ftp_path_args.ftp_path)
        filename = os.path.basename(ftp_path_args.ftp_path)
        dest_dir = os.path.abspath(local_path_args.local_path)
        if _is_file(ftp, ftp_path_args.ftp_path):
            with open(os.path.join(dest_dir, filename), "wb") as fd:
                ftp.retrbinary(f"RETR {ftp_path_args.ftp_path}", fd.write)
            return
        # maybe a directory
        ftp.cwd(dirname)
        ls = ftp.nlst()
        if filename not in ls:
            raise Exception(f"No such file/dir in FTP path: {dirname}")
        dirs = [
            (dest_dir, ftp_path_args.ftp_path),
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


def ftp_upload(args: argparse.Namespace) -> None:
    src_parser = argparse.ArgumentParser()
    src_parser.add_argument("src", nargs="+", help="source file/directory")
    dest_parser = argparse.ArgumentParser()
    dest_parser.add_argument(
        "dest",
        help=(
            "name of the destination directory where the files/directories "
            + "must be uploaded"
        ),
    )
    ui = cast(UI, args.ui)
    inp = ui.prompt_user("src paths: ", completer=PathCompleter())
    logging.debug(f"src paths entered by user: {inp}")
    src_args = src_parser.parse_args(inp.split())
    with FTPClient(get_selected_ftp_config()) as ftp:
        inp = ui.prompt_user("dest paths: ", completer=FTPPathCompleter(ftp))
        logging.debug(f"dest paths entered by user: {inp}")
        dst_args = dest_parser.parse_args(inp.split())
        for f in src_args.src:
            if os.path.isdir(f):
                ftp_recursive_upload(ftp, f, dst_args.dest)
                continue
            _upload(ftp, f, dst_args.dest)


def test(args: argparse.Namespace) -> None:
    with FTPClient(get_selected_ftp_config()) as ftp:
        logging.basicConfig(filename="test_ftp.log", level=logging.DEBUG)
        completer = CompleterAdaptor(FTPPathCompleter(ftp))
        print(
            prompt(
                "ftp path: ",
                completer=completer,
                complete_while_typing=False,
            )
        )


class FTPConfigDict(TypedDict):
    name: str
    username: str
    password: str
    host: str
    port: int


class State(TypedDict):
    selected_server: FTPConfigDict


class StateFileManager:
    def __init__(self, state_file: str = "") -> None:
        self._state_file_path = state_file or os.path.expanduser(
            "~/.var/pyftp/state_file.json"
        )
        state_file_dir = os.path.dirname(self._state_file_path)
        if not os.path.exists(state_file_dir):
            os.makedirs(state_file_dir, exist_ok=True)
        if not os.path.exists(self._state_file_path):
            with open(self._state_file_path, "w", encoding="utf-8") as fd:
                json.dump({"selected_server": {}}, fd)

    def get_state(self) -> State:
        with open(self._state_file_path, "r", encoding="utf-8") as fd:
            state = json.load(fd)
        return state

    def set_state(self, state: State) -> None:
        with open(self._state_file_path, "w", encoding="utf-8") as fd:
            json.dump(state, fd)


def select_ftp_server(args: argparse.Namespace) -> None:
    ftpconfigs = args.config_parser.parse()
    choice = args.ui.display_choice_menu(
        ls=list(map(lambda x: f"{x.name} ({x.host})", ftpconfigs)),
        title="FTP Servers:",
        prompt_str="Please choose FTP server: ",
    )
    ftpconfig = ftpconfigs[choice]
    state_manager = StateFileManager()
    state = state_manager.get_state()
    state["selected_server"] = cast(FTPConfigDict, asdict(ftpconfig))
    state_manager.set_state(state)


def get_selected_ftp_config() -> FTPConfig | None:
    state_manager = StateFileManager()
    state = state_manager.get_state()
    server = state["selected_server"]
    if len(server) == 0:
        return None
    return FTPConfig(
        name=server["name"],
        username=server["username"],
        password=server["password"],
        host=server["host"],
        port=server["port"],
    )


def main() -> None:
    logging.basicConfig(filename="test_ftp.log", level=logging.DEBUG)
    parser = argparse.ArgumentParser(description="ftp client")
    parser.set_defaults(func=lambda *x: parser.print_help())
    sub_parsers = parser.add_subparsers(description="FTP commands")
    select = sub_parsers.add_parser(
        "select",
        help=(
            "select the ftp server to connect to for processing subsequent"
            + "ftp operations"
        ),
    )
    select.set_defaults(
        func=select_ftp_server,
        config_parser=TomlFTPConfigParser("test_ftpconfig.toml"),
        # config_parser=FileZillaFTPConfigParser("/home/jprajwal/onedrive/workspace/docs/FileZilla.xml"),
        ui=PromptToolkitUI(),
    )
    ls = sub_parsers.add_parser(
        "ls", help="list files/directories in specified file/directory"
    )
    ls.set_defaults(func=ftp_ls, ui=PromptToolkitUI())
    download = sub_parsers.add_parser(
        "download", help="download files/directories"
    )
    download.set_defaults(func=ftp_download, ui=PromptToolkitUI())
    upload = sub_parsers.add_parser("upload", help="upload files/directories")
    upload.set_defaults(func=ftp_upload, ui=PromptToolkitUI())
    t = sub_parsers.add_parser("test", help="test autocompletion")
    t.set_defaults(func=test)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
