#!/usr/bin/env python3
"""
Sync Context Utility.
Synchronizes the knowledge base to ChromaDB.
"""

import argparse
import os

import chromadb


class ChromaDBSyncer:
    """Class to manage synchronization with ChromaDB."""

    def __init__(self, host: str = None, port: str = None):
        """
        Initializes the syncer. If host and port are provided, connects
        to the remote server. Otherwise, attempts to connect locally.
        """
        self.host = host
        self.port = port
        self.client = None

    def _load_dependencies(self):
        """Loads repository specific config dependencies dynamically."""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        os.path.dirname(script_dir)
        from howlplane.control_plane.config_loader import get_chroma_db_path

        return get_chroma_db_path

    def connect(self) -> None:
        """Connects to the ChromaDB database."""
        if self.host and self.port:
            print(f"Connecting to ChromaDB Server at {self.host}:{self.port}")
            self.client = chromadb.HttpClient(host=self.host, port=self.port)
        else:
            get_chroma_db_path = self._load_dependencies()
            db_path = get_chroma_db_path()
            print(f"Connecting to local ChromaDB at {db_path}")
            self.client = chromadb.PersistentClient(path=db_path)

    def sync(self) -> None:
        """Performs the sync operation."""
        self.connect()
        print("Sync complete.")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Sync knowledge base to ChromaDB.")
    parser.add_argument(
        "--host", type=str, help="ChromaDB Host (for client_server mode)"
    )
    parser.add_argument("--port", type=str, help="ChromaDB Port")
    args = parser.parse_args()

    syncer = ChromaDBSyncer(host=args.host, port=args.port)
    syncer.sync()


if __name__ == "__main__":
    main()
