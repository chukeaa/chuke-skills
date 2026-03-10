# Command Examples

## Basic checks
```bash
python3 scripts/synology_file_station.py check-config
python3 scripts/synology_file_station.py info
```

## Safety mode
By default, mutation commands are allowed because `SYNOLOGY_READONLY=false`.

To enforce read-only mode, set:
```bash
export SYNOLOGY_READONLY=true
```

To enable controlled writes, set an allowlist:
```bash
export SYNOLOGY_MUTATION_ALLOW_PATHS=/home/ai-work,/home/inbox
```
Only targets under allowlist roots can be modified.

## Browse files
```bash
python3 scripts/synology_file_station.py list-shares
python3 scripts/synology_file_station.py list --folder /home
python3 scripts/synology_file_station.py get-info --path /home/report.pdf
```

## Search
```bash
python3 scripts/synology_file_station.py search-start --folder /home --pattern "*.pdf"
python3 scripts/synology_file_station.py search-list --task-id <TASK_ID> --limit 200
python3 scripts/synology_file_station.py search-clean --task-id <TASK_ID>
```

## Create / rename
```bash
python3 scripts/synology_file_station.py mkdir --parent /home --name project-a
python3 scripts/synology_file_station.py rename --path /home/project-a --name project-alpha
```

## Copy / move / delete
```bash
python3 scripts/synology_file_station.py copy --path /home/a.txt --dest /home/archive --wait
python3 scripts/synology_file_station.py move --path /home/archive/a.txt --dest /home/final --wait
python3 scripts/synology_file_station.py delete --path /home/tmp --non-blocking --wait
```

## Upload / download
```bash
python3 scripts/synology_file_station.py upload --dest-folder /home/inbox --file ./local.txt
python3 scripts/synology_file_station.py download --path /home/inbox/local.txt --output ./downloads/
```

## Compress / extract
`compress` is temporarily unavailable in this skill.

```bash
python3 scripts/synology_file_station.py extract --archive /home/archive/inbox.zip --dest-folder /home/unpack --wait
```

## Background tasks
```bash
python3 scripts/synology_file_station.py background-list
python3 scripts/synology_file_station.py task-status --api copy-move --task-id <TASK_ID>
python3 scripts/synology_file_station.py task-stop --api copy-move --task-id <TASK_ID>
```
