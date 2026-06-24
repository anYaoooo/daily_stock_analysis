export interface CryptoTradingStatusResponse {
  exchange: string;
  configured: boolean;
  tradingEnabled: boolean;
  dryRun: boolean;
  sandbox: boolean;
  demoTrading: boolean;
  defaultSymbol: string;
  defaultType: string;
  tdMode: string;
  settle?: string | null;
}

export interface CryptoBalanceResponse {
  exchange: string;
  currency?: string | null;
  balance?: unknown;
  free?: number | null;
  used?: number | null;
  total?: number | null;
  raw?: unknown;
}

export interface CryptoListResponse {
  exchange: string;
  items: Record<string, unknown>[];
}

export type CryptoOrderType = 'market' | 'limit';
export type CryptoOrderSide = 'buy' | 'sell';
export type CryptoTradeMode = 'cash' | 'cross' | 'isolated';
export type CryptoPositionSide = 'long' | 'short' | 'net';
export type CryptoMarginMode = 'cross' | 'isolated';

export interface CryptoCreateOrderRequest {
  symbol?: string;
  orderType: CryptoOrderType;
  side: CryptoOrderSide;
  amount: number;
  price?: number;
  tdMode?: CryptoTradeMode;
  posSide?: CryptoPositionSide;
  reduceOnly?: boolean;
  clientOrderId?: string;
  extraParams?: Record<string, unknown>;
}

export interface CryptoCreateOrderResponse {
  dryRun: boolean;
  order: Record<string, unknown>;
}

export interface CryptoCancelOrderRequest {
  orderId: string;
  symbol?: string;
  extraParams?: Record<string, unknown>;
}

export interface CryptoCancelOrderResponse {
  dryRun: boolean;
  cancel: Record<string, unknown>;
}

export interface CryptoSetLeverageRequest {
  leverage: number;
  symbol?: string;
  marginMode?: CryptoMarginMode;
  posSide?: CryptoPositionSide;
}

export interface CryptoSetLeverageResponse {
  dryRun: boolean;
  leverage: Record<string, unknown>;
}

export interface CryptoSetMarginModeRequest {
  marginMode: CryptoMarginMode;
  symbol?: string;
}

export interface CryptoSetMarginModeResponse {
  dryRun: boolean;
  marginMode: Record<string, unknown>;
}
