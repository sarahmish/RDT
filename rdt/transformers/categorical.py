"""Transformers for categorical data."""

import numpy as np
import pandas as pd
import psutil
from scipy.stats import norm

from rdt.transformers.base import BaseTransformer


class CategoricalTransformer(BaseTransformer):
    """Transformer for categorical data.

    This transformer computes a float representative for each one of the categories
    found in the fit data, and then replaces the instances of these categories with
    the corresponding representative.

    The representatives are decided by sorting the categorical values by their relative
    frequency, then dividing the ``[0, 1]`` interval by these relative frequencies, and
    finally assigning the middle point of each interval to the corresponding category.

    When the transformation is reverted, each value is assigned the category that
    corresponds to the interval it falls in.

    Null values are considered just another category.

    Args:
        fuzzy (bool):
            Whether to generate gaussian noise around the class representative of each interval
            or just use the mean for all the replaced values. Defaults to ``False``.
        clip (bool):
            If ``True``, clip the values to [0, 1]. Otherwise normalize them using modulo 1.
            Defaults to ``False``.
    """

    mapping = None
    intervals = None
    starts = None
    means = None
    dtype = None
    _get_category_from_index = None

    def __setstate__(self, state):
        """Replace any ``null`` key by the actual ``np.nan`` instance."""
        intervals = state.get('intervals')
        if intervals:
            for key in list(intervals):
                if pd.isnull(key):
                    intervals[np.nan] = intervals.pop(key)

        self.__dict__ = state

    def __init__(self, fuzzy=False, clip=False):
        self.fuzzy = fuzzy
        self.clip = clip

    @staticmethod
    def _get_intervals(data):
        """Compute intervals for each categorical value.

        Args:
            data (pandas.Series):
                Data to analyze.

        Returns:
            dict:
                intervals for each categorical value (start, end).
        """
        frequencies = data.value_counts(dropna=False)

        start = 0
        end = 0
        elements = len(data)

        intervals = dict()
        means = []
        starts = []
        for value, frequency in frequencies.items():
            prob = frequency / elements
            end = start + prob
            mean = start + prob / 2
            std = prob / 6
            if pd.isnull(value):
                value = np.nan

            intervals[value] = (start, end, mean, std)
            means.append(mean)
            starts.append((value, start))
            start = end

        means = pd.Series(means, index=list(frequencies.keys()))
        starts = pd.DataFrame(starts, columns=['category', 'start']).set_index('start')

        return intervals, means, starts

    def fit(self, data):
        """Fit the transformer to the data.

        Create the mapping dict to save the label encoding.
        Finally, compute the intervals for each categorical value.

        Args:
            data (pandas.Series or numpy.ndarray):
                Data to fit the transformer to.
        """
        self.mapping = dict()
        self.dtype = data.dtype

        if isinstance(data, np.ndarray):
            data = pd.Series(data)

        self.intervals, self.means, self.starts = self._get_intervals(data)
        self._get_category_from_index = list(self.means.index).__getitem__

    def _transform_by_category(self, data):
        """Transform the data by iterating over the different categories."""
        result = np.empty(shape=(len(data), ), dtype=float)

        # loop over categories
        for category, values in self.intervals.items():
            mean, std = values[2:]
            if category is np.nan:
                mask = data.isnull()
            else:
                mask = (data.values == category)

            if self.fuzzy:
                result[mask] = norm.rvs(mean, std, size=mask.sum())
            else:
                result[mask] = mean

        return result

    def _get_value(self, category):
        """Get the value that represents this category."""
        if pd.isnull(category):
            category = np.nan

        mean, std = self.intervals[category][2:]

        if self.fuzzy:
            return norm.rvs(mean, std)

        return mean

    def _transform_by_row(self, data):
        """Transform the data row by row."""
        return data.fillna(np.nan).apply(self._get_value).to_numpy()

    def transform(self, data):
        """Transform categorical values to float values.

        Replace the categories with their float representative value.

        Args:
            data (pandas.Series or numpy.ndarray):
                Data to transform.

        Returns:
            numpy.ndarray:
        """
        if not isinstance(data, pd.Series):
            data = pd.Series(data)

        if len(self.means) < len(data):
            return self._transform_by_category(data)

        return self._transform_by_row(data)

    def _normalize(self, data):
        """Normalize data to the range [0, 1].

        This is done by either clipping or computing the values modulo 1.
        """
        if self.clip:
            return data.clip(0, 1)

        return np.mod(data, 1)

    def _reverse_transform_by_matrix(self, data):
        """Reverse transform the data with matrix operations."""
        num_rows = len(data)
        num_categories = len(self.means)

        data = np.broadcast_to(data, (num_categories, num_rows)).T
        means = np.broadcast_to(self.means, (num_rows, num_categories))
        diffs = np.abs(np.subtract(data, means))
        indexes = np.argmin(diffs, axis=1)

        self._get_category_from_index = list(self.means.index).__getitem__
        return pd.Series(indexes).apply(self._get_category_from_index).astype(self.dtype)

    def _reverse_transform_by_category(self, data):
        """Reverse transform the data by iterating over all the categories."""
        result = np.empty(shape=(len(data), ), dtype=self.dtype)

        # loop over categories
        for category, values in self.intervals.items():
            start = values[0]
            mask = (start <= data.values)
            result[mask] = category

        return pd.Series(result, index=data.index, dtype=self.dtype)

    def _get_category_from_start(self, value):
        lower = self.starts.loc[:value]
        return lower.iloc[-1].category

    def _reverse_transform_by_row(self, data):
        """Reverse transform the data by iterating over each row."""
        return data.apply(self._get_category_from_start).astype(self.dtype)

    def reverse_transform(self, data):
        """Convert float values back to the original categorical values.

        Args:
            data (numpy.ndarray):
                Data to revert.

        Returns:
            pandas.Series
        """
        if not isinstance(data, pd.Series):
            if len(data.shape) > 1:
                data = data[:, 0]

            data = pd.Series(data)

        data = self._normalize(data)

        num_rows = len(data)
        num_categories = len(self.means)

        # total shape * float size * number of matrices needed
        needed_memory = num_rows * num_categories * 8 * 3
        available_memory = psutil.virtual_memory().available
        if available_memory > needed_memory:
            return self._reverse_transform_by_matrix(data)

        if num_rows > num_categories:
            return self._reverse_transform_by_category(data)

        # loop over rows
        return self._reverse_transform_by_row(data)


class OneHotEncodingTransformer(BaseTransformer):
    """OneHotEncoding for categorical data.

    This transformer replaces a single vector with N unique categories in it
    with N vectors which have 1s on the rows where the corresponding category
    is found and 0s on the rest.

    Null values are considered just another category.

    Args:
        error_on_unknown (bool):
            If a value that was not seen during the fit stage is passed to
            transform, then an error will be raised if this is True.
    """

    dummy_na = None
    dummies = None

    def __init__(self, error_on_unknown=True):
        self.error_on_unknown = error_on_unknown

    @staticmethod
    def _prepare_data(data):
        """Transform data to appropriate format.

        If data is a valid list or a list of lists, transforms it into an np.array,
        otherwise returns it.

        Args:
            data (pandas.Series, numpy.ndarray, list or list of lists):
                Data to prepare.

        Returns:
            pandas.Series or numpy.ndarray
        """
        if isinstance(data, list):
            data = np.array(data)

        if len(data.shape) > 2:
            raise ValueError('Unexpected format.')
        if len(data.shape) == 2:
            if data.shape[1] != 1:
                raise ValueError('Unexpected format.')

            data = data[:, 0]

        return data

    def fit(self, data):
        """Fit the transformer to the data.

        Get the pandas `dummies` which will be used later on for OneHotEncoding.

        Args:
            data (pandas.Series, numpy.ndarray, list or list of lists):
                Data to fit the transformer to.
        """
        data = self._prepare_data(data)
        self.dummy_na = pd.isnull(data).any()
        self.dummies = list(pd.get_dummies(data, dummy_na=self.dummy_na).columns)

    def transform(self, data):
        """Replace each category with the OneHot vectors.

        Args:
            data (pandas.Series, numpy.ndarray, list or list of lists):
                Data to transform.

        Returns:
            numpy.ndarray:
        """
        data = self._prepare_data(data)
        dummies = pd.get_dummies(data, dummy_na=self.dummy_na)
        array = dummies.reindex(columns=self.dummies, fill_value=0).values.astype(int)
        for i, row in enumerate(array):
            if np.all(row == 0) and self.error_on_unknown:
                raise ValueError(f'The value {data[i]} was not seen during the fit stage.')

        return array

    def reverse_transform(self, data):
        """Convert float values back to the original categorical values.

        Args:
            data (numpy.ndarray):
                Data to revert.

        Returns:
            pandas.Series
        """
        if data.ndim == 1:
            data = data.reshape(-1, 1)

        indices = np.argmax(data, axis=1)
        return pd.Series(indices).map(self.dummies.__getitem__)


class LabelEncodingTransformer(BaseTransformer):
    """LabelEncoding for categorical data.

    This transformer generates a unique integer representation for each category
    and simply replaces each category with its integer value.

    Null values are considered just another category.

    Attributes:
        values_to_categories (dict):
            Dictionary that maps each integer value for its category.
        categories_to_values (dict):
            Dictionary that maps each category with the corresponding
            integer value.
    """

    values_to_categories = None
    categories_to_values = None

    def fit(self, data):
        """Fit the transformer to the data.

        Generate a unique integer representation for each category and
        store them in the `categories_to_values` dict and its reverse
        `values_to_categories`.

        Args:
            data (pandas.Series or numpy.ndarray):
                Data to fit the transformer to.
        """
        self.values_to_categories = dict(enumerate(data.unique()))
        self.categories_to_values = {
            category: value
            for value, category in self.values_to_categories.items()
        }

    def transform(self, data):
        """Replace each category with its corresponding integer value.

        Args:
            data (pandas.Series or numpy.ndarray):
                Data to transform.

        Returns:
            numpy.ndarray:
        """
        return data.map(self.categories_to_values)

    def reverse_transform(self, data):
        """Convert float values back to the original categorical values.

        Args:
            data (numpy.ndarray):
                Data to revert.

        Returns:
            pandas.Series
        """
        if isinstance(data, np.ndarray) and (data.ndim == 2):
            data = data[:, 0]

        data = data.clip(min(self.values_to_categories), max(self.values_to_categories))
        return pd.Series(data).round().map(self.values_to_categories)
