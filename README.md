# IDL reconstruction script

## Purpose

Decoding historical transactions / accounts based on Anchor IDLs requires to know the specific IDL for the program at
the time of the transaction.
This tool allows to reconstruct the on-chain IDL history for a given program.

## Method

The IDL reconstruction is done by aggregating the data chunks stored in the transactions writing to the IDL
buffers.
It uses the fact that such writes are done append-only, and looks for two distinct patterns:

- **IDL creation**, which entails the creation of the canonical IDL buffer and writing to it directly (effective release
  slot is then last write slot)
- **IDL upgrade**, which entails the creation of a temporary buffer, writing to it, and finally copying its content into
  the
  canonical IDL buffer (effective release slot is then the buffer copy slot)

## Usage

### Reconstruct IDL history for a specific program

```bash
python3 reconstruct_history.py --rpc_endpoint <your endpoint> --target_program_id <program id>
```

### Reconstruct IDL history for all programs currently deployed

```bash
python3 reconstruct_history.py --rpc_endpoint <your endpoint> --all
```

## Caveats

- The pattern detection could be too strict / incomplete, leading to false negatives
- In the context of historical decoding, the on-chain IDL reconstruction is only a piece of the puzzle, as IDL on-chain
  releases are rarely done atomically with the corresponding program upgrade. If this process is followed strictly from
  genesis, this tool will be enough to accurately decode the whole tx history. Otherwise, running this tool and patching
  the history is the next-best thing.
