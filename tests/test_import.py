import sys
import os

def test_start_kanban_server_importable():
    import scripts.start_kanban_server
    assert hasattr(scripts.start_kanban_server, 'read_board_data')
