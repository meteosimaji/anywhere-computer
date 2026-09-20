"""Compatibility entry point for the packaged catalog probe."""
from anywhere_computer.subchat_browser.catalog import (
    CONTROL as CONTROL,
)
from anywhere_computer.subchat_browser.catalog import (
    SOURCE as SOURCE,
)
from anywhere_computer.subchat_browser.catalog import (
    TOGGLE as TOGGLE,
)
from anywhere_computer.subchat_browser.catalog import (
    TRIGGER as TRIGGER,
)
from anywhere_computer.subchat_browser.catalog import (
    collect_page as collect_page,
)
from anywhere_computer.subchat_browser.catalog import (
    empty_chat as empty_chat,
)
from anywhere_computer.subchat_browser.catalog import (
    main,
)
from anywhere_computer.subchat_browser.catalog import (
    minimize_window as minimize_window,
)
from anywhere_computer.subchat_browser.catalog import (
    picker_ready as picker_ready,
)
from anywhere_computer.subchat_browser.catalog import (
    probe as probe,
)

if __name__ == "__main__":
    main()
