import os
import hashlib
import json
import zlib
import argparse
import concurrent.futures
import logging

from dataclasses import dataclass
from typing import List, Union, Tuple, Optional
from solana.rpc.api import Client
from solana.rpc.types import DataSliceOpts
from solana.rpc.commitment import Finalized
from solders.pubkey import Pubkey
from anchorpy.idl import IdlProgramAccount, IDL_ACCOUNT_LAYOUT
from solders.rpc.responses import GetTransactionResp
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from solders.transaction_status import EncodedConfirmedTransactionWithStatusMeta
from tqdm import tqdm

logging.getLogger().setLevel(logging.INFO)

# ---------------- #
# Helper functions #
# ---------------- #

def pako_inflate(data):
    decompress = zlib.decompressobj(15)
    decompressed_data = decompress.decompress(data)
    decompressed_data += decompress.flush()
    return decompressed_data


def resolve_idl_canonical_buffer(program_id: Pubkey) -> Pubkey:
    base = Pubkey.find_program_address([], program_id)[0]
    return Pubkey.create_with_seed(base, "anchor:idl", program_id)


def decode_idl_account(data: bytes) -> IdlProgramAccount:
    return IDL_ACCOUNT_LAYOUT.parse(data)


def get_transactions(
        client: Client,
        tx_sigs: List[Signature],
) -> List[EncodedConfirmedTransactionWithStatusMeta | None]:
    bodies = [client._get_transaction_body(sig, "base64", Finalized, 0) for sig in
              tx_sigs]
    resp = client._provider.make_batch_request_unparsed(bodies)
    resp_obj = json.loads(resp)
    txs = [GetTransactionResp.from_json(
        json.dumps(tx)
    ).value for tx in resp_obj]
    return txs


# ----------------------- #
# Reconstruction pipeline #
# ----------------------- #


@dataclass
class IdlCreateIdlAction:
    signature: Signature
    slot: int

    def __repr__(self):
        return f"IdlCreateIdlAction(slot={self.slot}, signature={self.signature})"


@dataclass
class IdlCreateBufferAction:
    buffer_address: Pubkey
    signature: Signature
    slot: int

    def __repr__(self):
        return f"IdlCreateBufferAction(buffer_address={self.buffer_address}, slot={self.slot}, signature={self.signature})"


@dataclass
class IdlWriteBufferAction:
    buffer_address: Pubkey
    data: bytes
    signature: Signature
    slot: int

    def __repr__(self):
        return f"IdlWriteBufferAction(buffer_address={self.buffer_address}, slot={self.slot}, signature={self.signature})"


@dataclass
class IdlSetBufferAction:
    buffer_address: Pubkey
    signature: Signature
    slot: int

    def __repr__(self):
        return f"IdlSetBufferAction(buffer_address={self.buffer_address}, slot={self.slot}, signature={self.signature})"


IdlAction = Union[IdlCreateIdlAction, IdlCreateBufferAction, IdlWriteBufferAction, IdlSetBufferAction]


@dataclass
class IdlUpdate:
    idl: str
    slot: int
    signature: Signature

    def __repr__(self):
        return f"IdlUpdate(slot={self.slot}, signature={self.signature}, idl_size={len(self.idl)})"


class IdlHistoryReconstructor:
    def __init__(self, rpc_client: Client, target_program_id: Pubkey, skip_confirm=False):
        self.target_program_id = target_program_id
        self.rpc_client = rpc_client
        self.idl_canonical_buffer = resolve_idl_canonical_buffer(self.target_program_id)
        self.skip_confirm = skip_confirm

        idl_ix_discriminator_base = hashlib.sha256("anchor:idl".encode()).digest()[:8]
        self.idl_ix_discriminator = int.from_bytes(idl_ix_discriminator_base, byteorder="big").to_bytes(8, byteorder="little")


    def reconstruct_idl_history(self):
        logging.debug(f"Reconstructing on-chain IDL history for {self.target_program_id} ...")
        txs = self.fetch_idl_buffer_transactions_chronological(self.idl_canonical_buffer)
        actions = self.extract_idl_actions(txs)
        idl_updates = self.reconstruct_idl_creations(actions) + self.reconstruct_idl_upgrades(actions)

        idl_updates.sort(key=lambda x: x.slot)

        if len(idl_updates) == 0:
            return
        
        logging.debug(f"Found {len(idl_updates)} versions")

        artifact_dir = f"generated/{self.target_program_id}"
        if not os.path.exists(artifact_dir):
            os.makedirs(artifact_dir)

        if not self.skip_confirm and os.listdir(artifact_dir):
            logging.warn("The generated folder is not empty. Delete all files? (y/n)")
            delete = input()
            if delete == "y":
                for file in os.listdir(artifact_dir):
                    os.remove(f"{artifact_dir}/{file}")
            else:
                logging.error("Aborting...")
                exit(1)

        for update in idl_updates:
            with open(f"{artifact_dir}/idl_{self.target_program_id}_{update.slot}.json", "w") as f:
                f.write(update.idl)


    # Fetch all transactions involving the IDL buffer
    def fetch_idl_buffer_transactions_chronological(self, buffer_address: Pubkey) -> List[
        EncodedConfirmedTransactionWithStatusMeta | None]:
        idl_sigs = self.rpc_client.get_signatures_for_address(buffer_address).value
        successful_idl_sigs = [sig.signature for sig in idl_sigs if sig.err is None]
        if len(successful_idl_sigs) == 0:
            return []
        
        idl_txs = get_transactions(self.rpc_client, successful_idl_sigs)
        idl_txs.reverse()
        return idl_txs



    # Filter and flatten all IDL-related instructions, in chronological order, to obtain an ordered list of
    # - idl creations
    # - buffer creations
    # - buffer writes
    # - buffer selections
    def extract_idl_actions(self, chronological_idl_transactions: List[EncodedConfirmedTransactionWithStatusMeta]) -> List[
        IdlAction]:
        idl_actions: List[IdlAction] = []
        for tx in chronological_idl_transactions:
            instructions_flattened = []
            transaction = tx.transaction.transaction
            message = transaction.message
            if isinstance(transaction, VersionedTransaction):
                for ix_index, ix in enumerate(message.instructions):
                    program_id = message.program_id(ix_index)
                    account_keys = message.account_keys
                    instructions_flattened.append({"program_id": program_id, "data": ix.data, "accounts": account_keys})
                    for inner_ix in tx.transaction.meta.inner_instructions:
                        inner_program_id = message.program_id(ix.program_id_index)
                        inner_account_keys = [account_keys[int(account_index)] for account_index in ix.accounts]
                        if inner_ix.index == ix_index:
                            instructions_flattened.extend(
                                [{"program_id": inner_program_id, "data": ix.data, "accounts": inner_account_keys} for ix in
                                inner_ix.instructions])
            else:
                logging.error("Not VersionedTransaction")

            for ix in instructions_flattened:
                if ix["program_id"] == self.target_program_id and ix["data"][:8] == self.idl_ix_discriminator:
                    enum_flag = ix["data"][8]
                    if enum_flag == 0:
                        idl_actions.append(
                            IdlCreateIdlAction(
                                signature=transaction.signatures[0],
                                slot=tx.slot))
                    if enum_flag == 1:
                        idl_actions.append(
                            IdlCreateBufferAction(buffer_address=ix["accounts"][1],
                                                # data=ix["data"][10:],
                                                signature=transaction.signatures[0],
                                                slot=tx.slot))
                    elif enum_flag == 2:
                        idl_actions.append(
                            IdlWriteBufferAction(buffer_address=ix["accounts"][1],
                                                data=ix["data"][13:],
                                                signature=transaction.signatures[0],
                                                slot=tx.slot))
                    elif enum_flag == 3:
                        idl_actions.append(
                            IdlSetBufferAction(buffer_address=ix["accounts"][1],
                                            signature=transaction.signatures[0],
                                            slot=tx.slot))
                else:
                    pass
                    # print("Found non-IDL ix")
                    # print(ix)

        return idl_actions



    # Reconstruct IDL upgrades from:
    # - 1 temporary buffer creation instruction
    # - a sequence of writes to that buffer
    # - 1 set buffer instruction
    #
    # The boundary slot / tx is considered to be the set buffer instruction, i.e. transaction after which the new IDL is
    # in effect.
    #
    # Note: this function currently discard an upgrade if the instruction sequence above is not strictly followed (e.g.
    # if there is any other instruction in between). This might be too strict, and could be relaxed in the future if
    # it proces to create false negatives.
    def reconstruct_idl_upgrades(self, actions: List[IdlAction]) -> List[IdlUpdate]:
        set_buffer_actions = [action for action in actions if
                            isinstance(action, IdlSetBufferAction) and action.buffer_address != self.idl_canonical_buffer]
        upgrades = []
        for a in set_buffer_actions:
            buffer_txs = self.fetch_idl_buffer_transactions_chronological(a.buffer_address)
            buffer_actions = self.extract_idl_actions(buffer_txs)

            if len(buffer_actions) < 3:
                logging.warn("Buffer too short")
                continue

            first_action = buffer_actions[0]
            if not isinstance(first_action, IdlCreateBufferAction):
                logging.warn("First action not create buffer")
                continue

            last_action = buffer_actions[-1]
            if not isinstance(last_action, IdlSetBufferAction):
                logging.warn("Last action not set buffer")
                continue

            inner_actions = buffer_actions[1:-1]
            if not all([isinstance(a, IdlWriteBufferAction) for a in inner_actions]):
                logging.warn("Inner actions not write buffer")
                continue

            idl = b"".join([a.data for a in inner_actions])
            inflated_idl = pako_inflate(idl).decode()

            upgrades.append(IdlUpdate(idl=inflated_idl, slot=last_action.slot, signature=last_action.signature))

        return upgrades


    # Reconstruct IDL creations from:
    # - 1 IDL creation instruction
    # - all direct writes to the IDL buffer until another IDL creation instruction
    #
    # The boundary slot / tx is considered to be the last write, i.e. transaction after which the IDL is complete and usable
    def reconstruct_idl_creations(self, actions: List[IdlAction]) -> List[IdlUpdate]:
        idl_creations = []
        data = b""
        last_write_slot_and_tx: Optional[Tuple[int, Signature]] = None
        for a in actions:
            if isinstance(a, IdlCreateIdlAction):
                if len(data) > 0 and last_write_slot_and_tx is not None:
                    inflated_idl = pako_inflate(data).decode()
                    idl_creations.append(
                        IdlUpdate(idl=inflated_idl, slot=last_write_slot_and_tx[0], signature=last_write_slot_and_tx[1]))
                    data = b""
                    last_write_slot_and_tx = None
            elif isinstance(a, IdlWriteBufferAction) and a.buffer_address == self.idl_canonical_buffer:
                data += a.data
                last_write_slot_and_tx = (a.slot, a.signature)

        return idl_creations


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--rpc_endpoint", required=True, help="RPC endpoint")
    parser.add_argument("--target_program_id", required=False, help="Target program ID")
    parser.add_argument("--all", action='store_true', help="Crawl all solana programs")
    args = parser.parse_args()
    
    if (args.target_program_id is None and args.all is None) or (args.target_program_id is not None and args.all is not None):
        logging.error("Need to specify either `--target_program_id` or `all`")
     
    rpc_client = Client(args.rpc_endpoint)

    if args.all:
        logging.info(f"Fetching all program accounts")
        upgradeable_program_id = Pubkey.from_string("BPFLoaderUpgradeab1e11111111111111111111111")
        program_accounts = rpc_client.get_program_accounts(upgradeable_program_id, data_slice=DataSliceOpts(offset=0, length=0)).value
        logging.info(f"Found {len(program_accounts)} program accounts")
        
        def process_program(index, program_account):
            logging.debug(f"Processing {program_account.pubkey} [{index}/{len(program_accounts)}]")
            reconstructor = IdlHistoryReconstructor(rpc_client, program_account.pubkey,  skip_confirm=True)
            reconstructor.reconstruct_idl_history()

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                pbar = tqdm(total=len(program_accounts))

                futures = {executor.submit(process_program, index, program_account) for index, program_account in enumerate(program_accounts)}
                
                def update(*args):
                    pbar.update()

                for future in futures:
                    future.add_done_callback(update)

                concurrent.futures.wait(futures)
                pbar.close()
        except KeyboardInterrupt:
            pbar.close()
            logging.info("Cancelling tasks ...")
            for future in futures:
                future.cancel()
    else:
        target_program_id = Pubkey.from_string(args.target_program_id)
        reconstructor = IdlHistoryReconstructor(rpc_client, target_program_id)
        reconstructor.reconstruct_idl_history()
