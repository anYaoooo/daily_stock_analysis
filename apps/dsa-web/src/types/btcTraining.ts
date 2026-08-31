export type BtcTrainingArchitecture = 'patchtst' | 'itransformer' | 'fusion' | 'all';

export interface BtcTrainingConfig {
  architectures: string[];
  defaultSeeds: number[];
  minSeeds: number;
  minEpochs: number;
  featureCount: number;
  featureColumns: string[];
  researchOnly: boolean;
  promotionEligible: boolean;
}

export interface BtcTrainingRunRequest {
  architecture: BtcTrainingArchitecture;
  epochs: number;
  seeds: number[];
  sequenceLength?: number;
  folds?: number;
  minTrainSamples?: number;
  validationSamples?: number;
  purgeSamples?: number;
  ablationFeatures?: string[];
}

export interface BtcTrainingTaskAccepted {
  taskId: string;
  status: string;
  message?: string;
  protocol: {
    sameLabels: boolean;
    sameValidationWindow: boolean;
    seedCount: number;
    epochs: number;
    architectures: string[];
    featureCount?: number;
    researchOnly: boolean;
    promotionEligible: boolean;
  };
}

export interface BtcTrainingRunResult {
  architecture: string;
  seed: number;
  featureSet: string;
  removedFeature?: string | null;
  featureCount: number;
  dataQuality?: string;
  oofPredictionCount: number;
  labelSignature?: string;
  validationWindowSignature?: string;
  evaluations?: Record<string, {
    foldCount?: number;
    samples?: number;
    directionAccuracy?: number | null;
    returnMae?: number | null;
    pearsonIc?: number | null;
    spearmanIc?: number | null;
  }>;
}

export interface BtcTrainingResearchResult {
  mode: string;
  researchOnly: boolean;
  participatesInDecision: boolean;
  eligibleForPromotion: boolean;
  promotionEligible: boolean;
  artifactPath?: string;
  oofArtifactPath?: string;
  artifactRole?: string;
  oofPredictionCount: number;
  protocol: {
    sameLabels: boolean;
    sameValidationWindow: boolean;
    labelSignature?: string;
    validationWindowSignature?: string;
    ablation: string;
    seedCount: number;
    seeds: number[];
    epochs: number;
    architectures: string[];
    featureCount: number;
    featureColumns: string[];
    ablationFeatures: string[];
  };
  summary: Record<string, Record<string, {
    runs: number;
    availableRuns: number;
    meanDirectionAccuracy?: number | null;
    meanReturnMae?: number | null;
    meanPearsonIc?: number | null;
    meanSpearmanIc?: number | null;
  }> >;
  runs: BtcTrainingRunResult[];
}

export interface BtcTrainingTaskStatus {
  taskId: string;
  status: string;
  progress: number;
  message?: string;
  result?: BtcTrainingResearchResult;
  error?: string;
}
