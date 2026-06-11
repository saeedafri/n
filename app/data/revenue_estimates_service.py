"""Compatibility wrapper for the legacy revenue estimates service path.

The page now uses RevenueForecastService from revenue_forecast_service. This
module keeps the historical import name alive for any older references.
"""

from data.revenue_forecast_service import RevenueForecastService

RevenueEstimatesService = RevenueForecastService

__all__ = ['RevenueForecastService', 'RevenueEstimatesService']
