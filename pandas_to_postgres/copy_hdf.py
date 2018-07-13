from .utilities import (
    create_file_object,
    df_generator,
    logger,
    cast_pandas,
    HDFMetadata,
)

from ._base_copy import BaseCopy
from typing import List
import pandas as pd
from sqlalchemy.sql.schema import Table
from sqlalchemy.engine.base import Connection


class HDFTableCopy(BaseCopy):
    """
    Class for handling a standard case of reading a table from an HDF file into a pandas
    DataFrame, iterating over it in chunks, and COPYing to PostgreSQL via StringIO CSV
    """

    def __init__(
        self,
        hdf_tables: List[str],
        hdf_meta: HDFMetadata,
        defer_sql_objs: bool = False,
        conn: Connection = None,
        table_obj: Table = None,
        sql_table: str = None,
        csv_chunksize: int = 10 ** 6,
    ):
        """
        Parameters
        ----------
        hdf_tables: list of HDF keys with data corresponding to destination SQL table
            (assumption being that HDF tables:SQL tables is many:one)
        hdf_meta: HDFMetadata object with information from the store
        defer_sql_objs: multiprocessing has issue with passing SQLALchemy objects, so if
            True, defer attributing these to the object until after pickled by Pool
        conn: SQLAlchemy connection managed outside of the object
        table_obj: SQLAlchemy object for the destination SQL Table
        sql_table: string of SQL table name
        csv_chunksize: max rows to keep in memory when generating CSV for COPY
        """
        super().__init__(defer_sql_objs, conn, table_obj, sql_table, csv_chunksize)

        self.hdf_tables = hdf_tables

        # Info from the HDFMetadata object
        self.hdf_metadata = hdf_meta.metadata_vars
        self.file_name = hdf_meta.file_name
        self.hdf_chunksize = hdf_meta.chunksize

    def copy(self, data_formatters=[cast_pandas], data_formatter_kwargs={}):
        """
        Go through sequence to COPY data to PostgreSQL table, including dropping Primary
        and Foreign Keys to optimize speed, TRUNCATE table, COPY data, recreate keys,
        and run ANALYZE.

        Parameters
        ----------
        data_formatters: list of functions to apply to df during sequence. Note that
            each of these functions should be able to handle kwargs for one another
        data_formatter_kwargs: list of kwargs to pass to data_formatters functions
        """
        self.drop_fks()
        self.drop_pk()

        # These need to be one transaction to use COPY FREEZE
        with self.conn.begin():
            self.truncate()
            self.hdf_to_pg(
                data_formatters=data_formatters,
                data_formatter_kwargs=data_formatter_kwargs,
            )

        self.create_pk()
        self.create_fks()
        self.analyze()

    def hdf_to_pg(self, data_formatters=[cast_pandas], data_formatter_kwargs={}):
        """
        Copy each HDF table that relates to SQL table to database

        Parameters
        ----------
        data_formatters: list of functions to apply to df during sequence. Note that
            each of these functions should be able to handle kwargs for one another
        data_formatter_kwargs: list of kwargs to pass to data_formatters functions
        """
        if self.hdf_tables is None:
            logger.warn(f"No HDF table found for SQL table {self.sql_table}")
            return

        for hdf_table in self.hdf_tables:
            logger.info(f"*** {hdf_table} ***")

            logger.info("Reading HDF table")
            df = pd.read_hdf(self.file_name, key=hdf_table)
            self.rows += len(df)

            data_formatter_kwargs["hdf_table"] = hdf_table
            logger.info("Formatting data")
            df = self.data_formatting(
                df, functions=data_formatters, **data_formatter_kwargs
            )

            logger.info("Creating generator for chunking dataframe")
            for chunk in df_generator(df, self.csv_chunksize):

                logger.info("Creating CSV in memory")
                fo = create_file_object(chunk)

                logger.info("Copying chunk to database")
                self.copy_from_file(fo)
                del fo
            del df
        logger.info(f"All chunks copied ({self.rows} rows)")


class SmallHDFTableCopy(HDFTableCopy):
    """
    Class for handling the case where the table is small enough to be stored completely
    in-memory for both reading from the HDF as well as COPYing using StringIO.
    """

    def __init__(
        self,
        hdf_tables: List[str],
        hdf_meta: HDFMetadata,
        defer_sql_objs: bool = False,
        conn: Connection = None,
        table_obj: Table = None,
        sql_table: str = None,
        csv_chunksize: int = 10 ** 6,
    ):
        """
        Parameters
        ----------
        hdf_tables: list of HDF keys with data corresponding to destination SQL table
            (assumption being that HDF tables:SQL tables is many:one)
        hdf_meta: HDFMetadata object with information from the store
        defer_sql_objs: multiprocessing has issue with passing SQLALchemy objects, so if
            True, defer attributing these to the object until after pickled by Pool
        conn: SQLAlchemy connection managed outside of the object
        table_obj: SQLAlchemy object for the destination SQL Table
        sql_table: string of SQL table name
        csv_chunksize: max rows to keep in memory when generating CSV for COPY
        """
        super().__init__(
            hdf_tables,
            hdf_meta,
            defer_sql_objs,
            conn,
            table_obj,
            sql_table,
            csv_chunksize,
        )

    def hdf_to_pg(self, data_formatters=[cast_pandas], data_formatter_kwargs={}):
        """
        Copy each HDF table that relates to SQL table to database

        Parameters
        ----------
        data_formatters: list of functions to apply to df during sequence. Note that
            each of these functions should be able to handle kwargs for one another
        data_formatter_kwargs: list of kwargs to pass to data_formatters functions
        """
        if self.hdf_tables is None:
            logger.warn("No HDF table found for SQL table {self.sql_table}")
            return

        for hdf_table in self.hdf_tables:
            logger.info(f"*** {hdf_table} ***")
            logger.info("Reading HDF table")
            df = pd.read_hdf(self.file_name, key=hdf_table)
            self.rows += len(df)

            data_formatter_kwargs["hdf_table"] = hdf_table
            logger.info("Formatting data")
            df = self.data_formatting(
                df, functions=data_formatters, **data_formatter_kwargs
            )

            logger.info("Creating CSV in memory")
            fo = create_file_object(df)

            logger.info("Copying table to database")
            self.copy_from_file(fo)
            del df
            del fo
        logger.info(f"All chunks copied ({self.rows} rows)")


class BigHDFTableCopy(HDFTableCopy):
    """
    Class for handling the special case of particularly large tables. For these, we
    iterate over reading the table in the HDF as well as iterating again over each of
    those chunks in order to keep the number of rows stored in-memory to a reasonable
    size. Note that these are iterated using pd.read_hdf(..., start, stop) rather than
    pd.read_hdf(..., iterator=True) because we found the performance was much better.
    """

    def __init__(
        self,
        hdf_tables: List[str],
        hdf_meta: HDFMetadata,
        defer_sql_objs: bool = False,
        conn: Connection = None,
        table_obj: Table = None,
        sql_table: str = None,
        csv_chunksize: int = 10 ** 6,
    ):
        """
        Parameters
        ----------
        hdf_tables: list of HDF keys with data corresponding to destination SQL table
            (assumption being that HDF tables:SQL tables is many:one)
        hdf_meta: HDFMetadata object with information from the store
        defer_sql_objs: multiprocessing has issue with passing SQLALchemy objects, so if
            True, defer attributing these to the object until after pickled by Pool
        conn: SQLAlchemy connection managed outside of the object
        table_obj: SQLAlchemy object for the destination SQL Table
        sql_table: string of SQL table name
        csv_chunksize: max rows to keep in memory when generating CSV for COPY
        """
        super().__init__(
            hdf_tables,
            hdf_meta,
            defer_sql_objs,
            conn,
            table_obj,
            sql_table,
            csv_chunksize,
        )

    def hdf_to_pg(self, data_formatters=[cast_pandas], data_formatter_kwargs={}):
        """
        Copy each HDF table that relates to SQL table to database

        Parameters
        ----------
        data_formatters: list of functions to apply to df during sequence. Note that
            each of these functions should be able to handle kwargs for one another
        data_formatter_kwargs: list of kwargs to pass to data_formatters functions
        """
        if self.hdf_tables is None:
            logger.warn(f"No HDF table found for SQL table {self.sql_table}")
            return

        for hdf_table in self.hdf_tables:
            logger.info(f"*** {hdf_table} ***")

            with pd.HDFStore(self.file_name) as store:
                nrows = store.get_storer(hdf_table).nrows

            self.rows += nrows
            if nrows % self.hdf_chunksize:
                n_chunks = (nrows // self.hdf_chunksize) + 1
            else:
                n_chunks = nrows // self.hdf_chunksize

            start = 0

            for i in range(n_chunks):
                logger.info(f"*** HDF chunk {i + 1} of {n_chunks} ***")
                logger.info("Reading HDF table")
                stop = min(start + self.hdf_chunksize, nrows)
                df = pd.read_hdf(self.file_name, key=hdf_table, start=start, stop=stop)

                start += self.hdf_chunksize

                data_formatter_kwargs["hdf_table"] = hdf_table
                logger.info("Formatting data")
                df = self.data_formatting(
                    df, functions=data_formatters, **data_formatter_kwargs
                )

                logger.info("Creating generator for chunking dataframe")
                for chunk in df_generator(df, self.csv_chunksize):
                    logger.info("Creating CSV in memory")
                    fo = create_file_object(chunk)

                    logger.info("Copying chunk to database")
                    self.copy_from_file(fo)
                    del fo
                del df
        logger.info(f"All chunks copied ({self.rows} rows)")
