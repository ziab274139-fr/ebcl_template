"""
tmux implementation of CommunicationInterface
"""
import libtmux

from . import CommunicationInterface


class TmuxSessionDoesNotExist(Exception):
    """ Raised if the expected tmux session is not found. """


class TmuxPaneNotInitialized(Exception):
    """ Raised if the tmux pane is none. """


class TmuxConsole(CommunicationInterface):
    """
    tmux implementation of CommunicationInterface
    """

    def __init__(self, session_name="console", window_name="bash"):
        super().__init__()
        self.l: int = 0
        self.server = libtmux.Server(socket_name="target")
        self.session_name = session_name
        self.window_name = window_name
        self.session = None
        self.window = None
        self.pane = None

    def connect(self):
        """
        Select session and window by names
        """
        if not self.session:
            self.session = self.server.sessions.get(
                session_name=self.session_name)

        if not self.session:
            raise TmuxSessionDoesNotExist(
                f'Tmux session {self.session_name} not found!')

        if not self.window:
            self.window = self.session.select_window(
                target_window=self.window_name)

        self.pane = self.window.active_pane

    def send_message(self, message: str):
        if not self.pane:
            raise TmuxPaneNotInitialized()

        self.connect()
        self.pane.clear()
        self.pane.send_keys(message, enter=True)
        self.l = 0

    def send_key(self, key: str):
        if not self.pane:
            raise TmuxPaneNotInitialized()

        self.connect()
        self.pane.send_keys(key, enter=False)
        self.l = 0

    def read_line(self):
        if not self.pane:
            raise TmuxPaneNotInitialized()

        self.connect()
        self.l += 1
        if self.l == 100:
            self.l = 0
        return " ".join(self.pane.capture_pane(start=self.l, end=self.l))

    def create_session(self, session_name: str):
        """
        Create new session and window

        Args:
            session_name (str): session name
            window_name (str): window name
        """
        s = self.server.has_session(session_name)
        if not s:
            self.session = self.server.new_session(session_name=session_name)
