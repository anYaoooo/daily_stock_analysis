import apiClient from './index';

export type ExtractItem = {
  code?: string | null;
  name?: string | null;
  confidence: string;
};

export type ExtractFromImageResponse = {
  codes: string[];
  items?: ExtractItem[];
  rawText?: string;
};

export type StockQuoteResponse = {
  stockCode: string;
  stockName?: string | null;
  currentPrice: number;
  change?: number | null;
  changePercent?: number | null;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  prevClose?: number | null;
  volume?: number | null;
  amount?: number | null;
  updateTime?: string | null;
  news?: StockNewsItem[];
};

export type StockNewsItem = {
  title: string;
  translatedTitle?: string | null;
  snippet?: string | null;
  summaryZh?: string | null;
  url?: string | null;
  source?: string | null;
  publishedDate?: string | null;
  relevanceScore?: number | null;
  relevanceCategory?: string | null;
  relevanceReasons: string[];
};

export type KLineData = {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number | null;
  amount?: number | null;
  changePercent?: number | null;
};

export type StockHistoryResponse = {
  stockCode: string;
  stockName?: string | null;
  period: string;
  data: KLineData[];
};

export type StockHistoryPeriod = 'hourly' | 'four_hour' | 'daily' | 'weekly' | 'monthly';

const mapQuote = (data: {
  stock_code?: string;
  stock_name?: string | null;
  current_price?: number;
  change?: number | null;
  change_percent?: number | null;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  prev_close?: number | null;
  volume?: number | null;
  amount?: number | null;
  update_time?: string | null;
  news?: Array<{
    title?: string;
    translated_title?: string | null;
    snippet?: string | null;
    summary_zh?: string | null;
    url?: string | null;
    source?: string | null;
    published_date?: string | null;
    relevance_score?: number | null;
    relevance_category?: string | null;
    relevance_reasons?: string[];
  }> | null;
}): StockQuoteResponse => ({
  stockCode: data.stock_code ?? '',
  stockName: data.stock_name,
  currentPrice: data.current_price ?? 0,
  change: data.change,
  changePercent: data.change_percent,
  open: data.open,
  high: data.high,
  low: data.low,
  prevClose: data.prev_close,
  volume: data.volume,
  amount: data.amount,
  updateTime: data.update_time,
  news: (data.news ?? []).map((item) => ({
    title: item.title ?? '',
    translatedTitle: item.translated_title,
    snippet: item.snippet,
    summaryZh: item.summary_zh,
    url: item.url,
    source: item.source,
    publishedDate: item.published_date,
    relevanceScore: item.relevance_score,
    relevanceCategory: item.relevance_category,
    relevanceReasons: item.relevance_reasons ?? [],
  })).filter((item) => item.title.trim().length > 0),
});

const mapKLine = (data: {
  date?: string;
  open?: number;
  high?: number;
  low?: number;
  close?: number;
  volume?: number | null;
  amount?: number | null;
  change_percent?: number | null;
}): KLineData => ({
  date: data.date ?? '',
  open: data.open ?? 0,
  high: data.high ?? 0,
  low: data.low ?? 0,
  close: data.close ?? 0,
  volume: data.volume,
  amount: data.amount,
  changePercent: data.change_percent,
});

export const stocksApi = {
  async getQuote(
    stockCode: string,
    options: { includeNews?: boolean } = {},
  ): Promise<StockQuoteResponse> {
    const response = await apiClient.get(`/api/v1/stocks/${encodeURIComponent(stockCode)}/quote`, {
      params: options.includeNews ? { include_news: true } : undefined,
    });
    return mapQuote(response.data);
  },

  async getHistory(
    stockCode: string,
    options: { period?: StockHistoryPeriod; days?: number } = {},
  ): Promise<StockHistoryResponse> {
    const response = await apiClient.get(`/api/v1/stocks/${encodeURIComponent(stockCode)}/history`, {
      params: {
        period: options.period ?? 'daily',
        days: options.days ?? 30,
      },
    });
    const data = response.data as {
      stock_code?: string;
      stock_name?: string | null;
      period?: string;
      data?: Array<{
        date?: string;
        open?: number;
        high?: number;
        low?: number;
        close?: number;
        volume?: number | null;
        amount?: number | null;
        change_percent?: number | null;
      }>;
    };
    return {
      stockCode: data.stock_code ?? stockCode,
      stockName: data.stock_name,
      period: data.period ?? options.period ?? 'daily',
      data: (data.data ?? []).map(mapKLine),
    };
  },

  async extractFromImage(file: File): Promise<ExtractFromImageResponse> {
    const formData = new FormData();
    formData.append('file', file);

    const headers: { [key: string]: string | undefined } = { 'Content-Type': undefined };
    const response = await apiClient.post(
      '/api/v1/stocks/extract-from-image',
      formData,
      {
        headers,
        timeout: 60000, // Vision API can be slow; 60s
      },
    );

    const data = response.data as { codes?: string[]; items?: ExtractItem[]; raw_text?: string };
    return {
      codes: data.codes ?? [],
      items: data.items,
      rawText: data.raw_text,
    };
  },

  async parseImport(file?: File, text?: string): Promise<ExtractFromImageResponse> {
    if (file) {
      const formData = new FormData();
      formData.append('file', file);
      const headers: { [key: string]: string | undefined } = { 'Content-Type': undefined };
      const response = await apiClient.post('/api/v1/stocks/parse-import', formData, { headers });
      const data = response.data as { codes?: string[]; items?: ExtractItem[] };
      return { codes: data.codes ?? [], items: data.items };
    }
    if (text) {
      const response = await apiClient.post('/api/v1/stocks/parse-import', { text });
      const data = response.data as { codes?: string[]; items?: ExtractItem[] };
      return { codes: data.codes ?? [], items: data.items };
    }
    throw new Error('请提供文件或粘贴文本');
  },
};
