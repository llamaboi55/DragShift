from pathlib import Path
from types import SimpleNamespace
from dragshift.trigger import DragShiftWatcher
from dragshift.selection import selected_paths


def test_shift_tap_requests_selection_without_drag_or_click(qt_session,monkeypatch):
    watcher=DragShiftWatcher(lambda:False,lambda:False)
    state=[False,False,False]
    monkeypatch.setattr(watcher,'_state',lambda:tuple(state))
    monkeypatch.setattr(watcher,'_other_key_down',lambda:False)
    events=[]
    watcher.selection_menu.connect(lambda _:events.append('selection'))
    state[1]=True
    watcher._tick()
    assert not events
    state[1]=False
    watcher._tick()
    assert events==['selection']
    state[:]=[False,True,False]
    monkeypatch.setattr(watcher,'_other_key_down',lambda:True)
    watcher._tick()
    state[1]=False
    watcher._tick()
    assert events==['selection']  # Shift+typing/arrows is never a shortcut.
    # Holding Shift during a mouse click stays an ordinary OS range selection.
    state[:]=[False,True,False]
    watcher._tick()
    state[0]=True
    watcher._tick()
    state[:]=[False,False,False]
    watcher._tick()
    assert events==['selection']


def test_selection_reader_does_not_fill_gaps_or_select_neighbors(tmp_path):
    paths=[tmp_path/name for name in ('first.txt','neighbor.txt','last.txt')]
    for path in paths:
        path.write_text('test')
    items=[SimpleNamespace(Path=str(paths[n]),IsFileSystem=True) for n in (0,2)]
    view=SimpleNamespace(Document=SimpleNamespace(SelectedItems=lambda:items))
    assert selected_paths(view)==[str(paths[0]),str(paths[2])]
