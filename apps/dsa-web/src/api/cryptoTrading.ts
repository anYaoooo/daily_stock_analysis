import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  CryptoBalanceResponse,
  CryptoCancelOrderRequest,
  CryptoCancelOrderResponse,
  CryptoCreateOrderRequest,
  CryptoCreateOrderResponse,
  CryptoListResponse,
  CryptoSetLeverageRequest,
  CryptoSetLeverageResponse,
  CryptoSetMarginModeRequest,
  CryptoSetMarginModeResponse,
  CryptoTradingStatusResponse,
} from '../types/cryptoTrading';

const toOrderPayload = (payload: CryptoCreateOrderRequest): Record<string, unknown> => ({
  symbol: payload.symbol,
  order_type: payload.orderType,
  side: payload.side,
  amount: payload.amount,
  price: payload.price,
  td_mode: payload.tdMode,
  pos_side: payload.posSide,
  reduce_only: payload.reduceOnly ?? false,
  client_order_id: payload.clientOrderId,
  extra_params: payload.extraParams ?? {},
});

const toCancelPayload = (payload: CryptoCancelOrderRequest): Record<string, unknown> => ({
  order_id: payload.orderId,
  symbol: payload.symbol,
  extra_params: payload.extraParams ?? {},
});

const toLeveragePayload = (payload: CryptoSetLeverageRequest): Record<string, unknown> => ({
  leverage: payload.leverage,
  symbol: payload.symbol,
  margin_mode: payload.marginMode,
  pos_side: payload.posSide,
});

const toMarginModePayload = (payload: CryptoSetMarginModeRequest): Record<string, unknown> => ({
  margin_mode: payload.marginMode,
  symbol: payload.symbol,
});

export const cryptoTradingApi = {
  async getStatus(): Promise<CryptoTradingStatusResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/crypto-trading/status');
    return toCamelCase<CryptoTradingStatusResponse>(response.data);
  },

  async getBalance(currency = 'USDT'): Promise<CryptoBalanceResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/crypto-trading/balance', {
      params: { currency },
    });
    return toCamelCase<CryptoBalanceResponse>(response.data);
  },

  async getPositions(symbol = 'BTC'): Promise<CryptoListResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/crypto-trading/positions', {
      params: { symbol },
    });
    return toCamelCase<CryptoListResponse>(response.data);
  },

  async getOpenOrders(symbol = 'BTC'): Promise<CryptoListResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/crypto-trading/orders/open', {
      params: { symbol },
    });
    return toCamelCase<CryptoListResponse>(response.data);
  },

  async createOrder(payload: CryptoCreateOrderRequest): Promise<CryptoCreateOrderResponse> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/crypto-trading/orders', toOrderPayload(payload));
    return toCamelCase<CryptoCreateOrderResponse>(response.data);
  },

  async cancelOrder(payload: CryptoCancelOrderRequest): Promise<CryptoCancelOrderResponse> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/crypto-trading/orders/cancel', toCancelPayload(payload));
    return toCamelCase<CryptoCancelOrderResponse>(response.data);
  },

  async setLeverage(payload: CryptoSetLeverageRequest): Promise<CryptoSetLeverageResponse> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/crypto-trading/leverage', toLeveragePayload(payload));
    return toCamelCase<CryptoSetLeverageResponse>(response.data);
  },

  async setMarginMode(payload: CryptoSetMarginModeRequest): Promise<CryptoSetMarginModeResponse> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/crypto-trading/margin-mode', toMarginModePayload(payload));
    return toCamelCase<CryptoSetMarginModeResponse>(response.data);
  },
};
