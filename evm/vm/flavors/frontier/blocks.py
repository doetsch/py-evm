import rlp
from rlp.sedes import (
    CountableList,
)

from eth_bloom import (
    BloomFilter,
)

from trie import (
    Trie,
)

from evm.constants import (
    EMPTY_UNCLE_HASH,
)
from evm.exceptions import (
    InvalidBlock,
)
from evm.rlp.logs import (
    Log,
)
from evm.rlp.receipts import (
    Receipt,
)
from evm.rlp.blocks import (
    BaseBlock,
)
from evm.rlp.headers import (
    BlockHeader,
)

from evm.utils.transactions import (
    get_transactions_from_db,
)
from evm.utils.receipts import (
    get_receipts_from_db,
)

from .transactions import (
    FrontierTransaction,
)


class FrontierBlock(BaseBlock):
    fields = [
        ('header', BlockHeader),
        ('transactions', CountableList(FrontierTransaction)),
        ('uncles', CountableList(BlockHeader))
    ]

    db = None
    bloom_filter = None

    def __init__(self, header, transactions=None, uncles=None, db=None):
        if db is not None:
            self.db = db

        if self.db is None:
            raise TypeError("Block must have a db")

        if transactions is None:
            transactions = []
        if uncles is None:
            uncles = []

        self.bloom_filter = BloomFilter(header.bloom)
        self.transaction_db = Trie(db=self.db, root_hash=header.transaction_root)
        self.receipt_db = Trie(db=self.db, root_hash=header.receipt_root)

        super(FrontierBlock, self).__init__(
            header=header,
            transactions=transactions,
            uncles=uncles,
        )
        # TODO: should perform block validation at this point?

    def validate(self):
        if not self.is_genesis:
            parent_block = self.get_parent()

            # timestamp
            if self.header.timestamp < parent_block.header.timestamp:
                raise InvalidBlock("Block timestamp is before the parent block's timestamp")
            elif self.header.timestamp == parent_block.header.timestamp:
                raise InvalidBlock("Block timestamp is equal to the parent block's timestamp")

        super(FrontierBlock, self).validate()

    #
    # Helpers
    #
    @property
    def number(self):
        return self.header.block_number

    def get_parent_header(self):
        parent_header = rlp.decode(
            self.db.get(self.header.parent_hash),
            sedes=BlockHeader,
        )
        return parent_header

    def get_parent(self):
        parent_header = self.get_parent_header()
        return self.from_header(parent_header)

    #
    # Transaction class for this block class
    #
    transaction_class = FrontierTransaction

    @classmethod
    def get_transaction_class(cls):
        return cls.transaction_class

    #
    # Gas Usage API
    #
    def get_cumulative_gas_used(self):
        """
        Note return value of this function can be cached based on
        `self.receipt_db.root_hash`
        """
        if len(self.transactions):
            return self.receipts[-1].gas_used
        else:
            return 0

    #
    # Receipts API
    #
    @property
    def receipts(self):
        return get_receipts_from_db(self.receipt_db, Receipt)

    #
    # Header API
    #
    @classmethod
    def from_header(cls, header):
        """
        Returns the block denoted by the given block header.
        """
        if header.uncles_hash == EMPTY_UNCLE_HASH:
            uncles = []
        else:
            uncles = rlp.decode(cls.db.get(header.uncles_hash), sedes=CountableList(BlockHeader))

        transaction_db = Trie(cls.db, root_hash=header.transaction_root)
        transactions = get_transactions_from_db(transaction_db, cls.get_transaction_class())

        return cls(
            header=header,
            transactions=transactions,
            uncles=uncles,
        )

    #
    # Execution API
    #
    def apply_transaction(self, evm, transaction):
        computation = evm.apply_transaction(transaction)

        logs = [
            Log(address, topics, data)
            for address, topics, data
            in computation.get_log_entries()
        ]

        if computation.error:
            gas_used = self.get_cumulative_gas_used() + transaction.gas
        else:
            gas_remaining = computation.get_gas_remaining()
            base_gas_used = transaction.gas - gas_remaining
            gas_refunded = min(
                computation.get_gas_refund(),
                base_gas_used // 2,
            )
            gas_used = self.get_cumulative_gas_used() + base_gas_used - gas_refunded

        receipt = Receipt(
            state_root=computation.state_db.root_hash,
            gas_used=gas_used,
            logs=logs,
        )

        transaction_idx = len(self.transactions)

        index_key = rlp.encode(transaction_idx, sedes=rlp.sedes.big_endian_int)

        self.transactions.append(transaction)

        self.transaction_db[index_key] = rlp.encode(transaction)
        self.receipt_db[index_key] = rlp.encode(receipt)

        self.bloom_filter |= receipt.bloom

        self.header.transaction_root = self.transaction_db.root_hash
        self.header.state_root = computation.state_db.root_hash
        self.header.receipt_root = self.receipt_db.root_hash
        self.header.bloom = int(self.bloom_filter)
        self.header.gas_used = gas_used

        return computation

    def mine(self, **kwargs):
        """
        - `uncles_hash`
        - `state_root`
        - `transaction_root`
        - `receipt_root`
        - `bloom`
        - `gas_used`
        - `extra_data`
        - `mix_hash`
        - `nonce`
        """
        header = self.header
        provided_fields = set(kwargs.keys())
        known_fields = set(tuple(zip(*BlockHeader.fields))[0])
        unknown_fields = provided_fields.difference(known_fields)

        if unknown_fields:
            raise AttributeError(
                "Unable to set the field(s) {0} on the `BlockHeader` class. "
                "Received the following unexpected fields: {0}.".format(
                    ", ".join(unknown_fields),
                    ", ".join(known_fields),
                )
            )

        for key, value in kwargs.items():
            setattr(header, key, value)

        # TODO: do we validate here!?
        return self
