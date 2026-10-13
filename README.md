# wc3-save-pc-to-mac

Makes Warcraft III Reforged saves from Windows load on macOS.

On Mac, a save made on Windows fails with *"There was an error trying to load the selected save game"*,
or the game crashes. The Windows and Mac builds write some pathing data in the save differently.
This tool rewrites that data in the Mac format. Everything else in the save stays byte for byte.

Tested with campaign saves on Warcraft III Reforged 3.0.0.24268 (Mac).

## Usage

Needs Python 3.9+, no other dependencies.

1. Copy the Windows `.w3z` saves to your Mac.
2. Convert them into a folder of your choice:

   ```sh
   python3 convert.py ~/Desktop/converted "Treachery.w3z" "Act Two - Undercity.w3z"
   ```

   ```
   Treachery.w3z -> ok, 1369 objects converted
   Act Two - Undercity.w3z -> ok, 1478 objects converted
   ```

3. Put the converted files in the Mac save folder, for example for a campaign:
   `~/Library/Application Support/Warcraft III/BattleNet/<account-id>/Campaigns/<campaign>/`.
   Give them a new name if a file with the same name is already there.
4. Load them in the game.

The tool never overwrites files and never changes the input saves. If a save doesn't match the format
it expects (for example, a save that is already a Mac save), it prints `FAILED` and writes nothing.
Keep your originals anyway.

## What is different

Some pathing objects hold a table. Windows writes the table header with 4 numbers, Mac expects 5:

```
Windows: [0][C][D][N]     + N entries
Mac:     [A][A][C][D][N]  + N entries     A = D * entry size in memory
```

The Mac game reads everything after this header one number off, so the load check fails.
`convert.py` finds these objects and rewrites their headers. `w3z.py` reads and writes the `.w3z` file
container (zlib blocks with checksums).
