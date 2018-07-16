import logging
from pandas import HDFStore, isna
from collections import defaultdict
from io import StringIO


logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)s %(asctime)s.%(msecs)03d %(message)s",
    datefmt="%Y-%m-%d,%H:%M:%S",
)

logger = logging.getLogger("pandas_to_postgres")


class HDFMetadata(object):
    """Collect applicable metadata from HDFStore to use when running copy"""

    def __init__(
        self,
        file_name,
        keys=None,
        chunksize=10 ** 7,
        metadata_attr=None,
        metadata_keys=[],
    ):
        self.file_name = file_name
        self.chunksize = chunksize
        self.sql_to_hdf = defaultdict(set)
        self.metadata_vars = defaultdict(dict)
        """
        Parameters
        ----------
        file_name: str
            path to hdf file to copy from
        keys: list of strings
            HDF keys to copy data from
        chunksize: int
            Maximum rows read from an hdf file into a pandas dataframe when using
            the BigTable protocol
        metadata_attr: str
            :ocation of relevant metadata in store.get_storer().attrs
        metadata_keys: list of strings
            Keys to get from metadata store
        """

        with HDFStore(self.file_name, mode="r") as store:
            self.keys = keys or store.keys()

            if metadata_attr:
                for key in self.keys:
                    try:
                        metadata = store.get_storer(key).attrs[metadata_attr]
                        logger.info("Metadata: {}".format(metadata))
                    except (AttributeError, KeyError):
                        if "/meta" not in key:
                            logger.info(
                                "No metadata found for key '{}'. Skipping".format(key)
                            )
                        continue

                    for mkey in metadata_keys:
                        self.metadata_vars[mkey][key] = metadata.get(mkey)

                    sql_table = metadata.get("sql_table_name")

                    if sql_table:
                        self.sql_to_hdf[sql_table].add(key)
                    else:
                        logger.warn("No SQL table name found for {}".format(key))


def create_file_object(df):
    """
    Writes pandas dataframe to an in-memory StringIO file object. Adapted from
    https://gist.github.com/mangecoeur/1fbd63d4758c2ba0c470#gistcomment-2086007

    Parameters
    ----------
    df: pandas DataFrame

    Returns
    -------
    file_object: StringIO
    """
    file_object = StringIO()
    df.to_csv(file_object, index=False)
    file_object.seek(0)
    return file_object


def df_generator(df, chunksize=10 ** 6):
    """
    Create a generator to iterate over chunks of a dataframe

    Parameters
    ----------
    df: pandas dataframe
        Data to iterate over
    chunksize: int
        Max number of rows to return in a chunk
    """
    rows = 0
    if not df.shape[0] % chunksize:
        n_chunks = max(df.shape[0] // chunksize, 1)
    else:
        n_chunks = (df.shape[0] // chunksize) + 1

    for i in range(n_chunks):
        logger.info("Chunk {i}/{n}".format(i=i + 1, n=n_chunks))
        yield df.iloc[rows : rows + chunksize]
        rows += chunksize


def cast_pandas(df, columns=None, copy_obj=None, **kwargs):
    """
    Pandas does not handle null values in integer or boolean fields out of the
    box, so cast fields that should be these types in the database to object
    fields and change np.nan to None

    Parameters
    ----------
    df: pandas DataFrame
        data frame with fields that are desired to be int or bool as float with
        np.nan that should correspond to None
    columns: list of SQLAlchemy Columns
        Columnsto iterate through to determine data types
    copy_obj: BaseCopy or subclass
        instance of BaseCopy passed from the BaseCopy.data_formatting method where
        we can access BaseCopy.table_obj.columns

    Returns
    -------
    df: pandas DataFrame
        DataFrame with fields that correspond to Postgres int, bigint, and bool
        fields changed to objects with None values for null
    """

    if columns is None and copy_obj is None:
        raise ValueError("One of columns or copy_obj must be supplied")

    columns = columns or copy_obj.table_obj.columns
    for col in columns:
        if str(col.type) in ["INTEGER", "BIGINT"]:
            df[col.name] = df[col.name].apply(
                lambda x: None if isna(x) else int(x), convert_dtype=False
            )
        elif str(col.type) == "BOOLEAN":
            df[col.name] = df[col.name].apply(
                lambda x: None if isna(x) else bool(x), convert_dtype=False
            )

    return df
