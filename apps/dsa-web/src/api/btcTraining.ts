import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  BtcTrainingConfig,
  BtcTrainingRunRequest,
  BtcTrainingTaskAccepted,
  BtcTrainingTaskStatus,
} from '../types/btcTraining';

export const btcTrainingApi = {
  getConfig: async (): Promise<BtcTrainingConfig> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/btc-training/config');
    return toCamelCase<BtcTrainingConfig>(response.data);
  },

  run: async (params: BtcTrainingRunRequest): Promise<BtcTrainingTaskAccepted> => {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/btc-training/run', {
      architecture: params.architecture,
      epochs: params.epochs,
      seeds: params.seeds,
      sequence_length: params.sequenceLength,
      folds: params.folds,
      min_train_samples: params.minTrainSamples,
      validation_samples: params.validationSamples,
      purge_samples: params.purgeSamples,
      ablation_features: params.ablationFeatures ?? [],
    });
    return toCamelCase<BtcTrainingTaskAccepted>(response.data);
  },

  getTask: async (taskId: string): Promise<BtcTrainingTaskStatus> => {
    const response = await apiClient.get<Record<string, unknown>>(`/api/v1/btc-training/tasks/${encodeURIComponent(taskId)}`);
    return toCamelCase<BtcTrainingTaskStatus>(response.data);
  },

  downloadArtifact: async (taskId: string, kind: 'summary' | 'oof'): Promise<void> => {
    const response = await apiClient.get<Blob>(
      `/api/v1/btc-training/tasks/${encodeURIComponent(taskId)}/artifacts/${kind}`,
      { responseType: 'blob' },
    );
    const disposition = String(response.headers['content-disposition'] ?? '');
    const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
    const plainName = disposition.match(/filename="?([^";]+)"?/i)?.[1];
    const fallbackName = kind === 'oof' ? `btc-training-${taskId}-oof.jsonl` : `btc-training-${taskId}.json`;
    const fileName = encodedName ? decodeURIComponent(encodedName) : plainName || fallbackName;
    const url = URL.createObjectURL(response.data);
    const link = document.createElement('a');
    link.href = url;
    link.download = fileName;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  },
};
