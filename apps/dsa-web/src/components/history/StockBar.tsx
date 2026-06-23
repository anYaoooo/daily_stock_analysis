import type React from 'react';
import { useState, useCallback, useRef, useEffect, useId } from 'react';
import { PanelLeftClose } from 'lucide-react';
import { Badge, Button, ScrollArea } from '../common';
import { DashboardPanelHeader, DashboardStateBlock } from '../dashboard';
import { StockBarItemComponent } from './StockBarItem';
import type { StockBarItem as StockBarItemType } from '../../types/analysis';
import { useUiLanguage } from '../../contexts/UiLanguageContext';

interface StockBarProps {
  items: StockBarItemType[];
  isLoading: boolean;
  selectedStockCode?: string;
  selectedRecordId?: number;
  onItemClick: (recordId: number) => void;
  onDeleteRecord?: (recordId: number) => Promise<void> | void;
  onClose?: () => void;
  isDeleting?: boolean;
  className?: string;
}

/**
 * 个股栏组件：展示历史分析记录，同一标的可以保留多条记录。
 * 大盘复盘可作为 MARKET 项参与展示，并按最近分析时间排序。
 */
export const StockBar: React.FC<StockBarProps> = ({
  items,
  isLoading,
  selectedStockCode,
  selectedRecordId,
  onItemClick,
  onDeleteRecord,
  onClose,
  isDeleting = false,
  className = '',
}) => {
  const { t } = useUiLanguage();
  const isMarketReview = (code: string) => code === 'MARKET';
  const [selectedRecordIds, setSelectedRecordIds] = useState<Set<number>>(new Set());
  const selectAllRef = useRef<HTMLInputElement>(null);
  const selectAllId = useId();

  const deletableItems = items;
  const selectedCount = [...selectedRecordIds].filter((id) => deletableItems.some((item) => item.id === id)).length;
  const allVisibleSelected = deletableItems.length > 0 && selectedCount === deletableItems.length;
  const someVisibleSelected = selectedCount > 0 && !allVisibleSelected;

  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = someVisibleSelected;
    }
  }, [someVisibleSelected]);

  const toggleRecord = useCallback((recordId: number) => {
    setSelectedRecordIds((prev) => {
      const next = new Set(prev);
      if (next.has(recordId)) next.delete(recordId);
      else next.add(recordId);
      return next;
    });
  }, []);

  const toggleSelectAll = useCallback(() => {
    setSelectedRecordIds(allVisibleSelected ? new Set() : new Set(deletableItems.map((item) => item.id)));
  }, [allVisibleSelected, deletableItems]);

  const handleDeleteSelected = useCallback(async () => {
    if (!onDeleteRecord || selectedRecordIds.size === 0) return;
    const recordIdsToDelete = [...selectedRecordIds];
    for (const recordId of recordIdsToDelete) {
      await onDeleteRecord(recordId);
    }
    setSelectedRecordIds(new Set());
  }, [onDeleteRecord, selectedRecordIds]);

  return (
    <aside className={`glass-card overflow-hidden flex flex-col ${className}`}>
      <ScrollArea
        viewportClassName="p-4"
        testId="home-stock-bar-scroll"
      >
        <div className="mb-4 space-y-3">
          <DashboardPanelHeader
            className="mb-1"
            title={t('stockBar.title')}
            titleClassName="text-sm font-medium"
            leading={(
              <svg className="h-4 w-4 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
              </svg>
            )}
            headingClassName="items-center"
            actions={
              <div className="flex items-center gap-1.5">
                {selectedCount > 0 ? (
                  <Badge variant="info" size="sm" className="animate-in fade-in zoom-in duration-200">
                    {t('common.selectedCount', { count: selectedCount })}
                  </Badge>
                ) : items.length > 0 ? (
                  <span className="text-[11px] text-muted-text">{t('common.itemsCount', { count: items.length })}</span>
                ) : null}
                {onClose ? (
                  <Button
                    variant="ghost"
                    size="xsm"
                    onClick={onClose}
                    className="h-6 w-6 p-0"
                    aria-label={t('stockBar.close')}
                    title={t('stockBar.close')}
                  >
                    <PanelLeftClose className="h-3.5 w-3.5" aria-hidden="true" />
                  </Button>
                ) : null}
              </div>
            }
          />

          {items.length > 0 && onDeleteRecord && (
            <div className="flex items-center gap-2">
              <label
                className="flex flex-1 cursor-pointer items-center gap-2 rounded-lg px-2 py-1"
                htmlFor={selectAllId}
              >
                <input
                  id={selectAllId}
                  ref={selectAllRef}
                  type="checkbox"
                  checked={allVisibleSelected}
                  onChange={toggleSelectAll}
                  disabled={isDeleting}
                  aria-label={t('history.selectAllStockRecordAria')}
                  className="h-3.5 w-3.5 cursor-pointer bg-transparent accent-primary focus:ring-primary/30 disabled:opacity-50"
                />
                <span className="text-[11px] text-muted-text select-none">{t('common.selectAllCurrent')}</span>
              </label>
              <Button
                variant="danger-subtle"
                size="xsm"
                onClick={() => void handleDeleteSelected()}
                disabled={selectedCount === 0 || isDeleting}
                isLoading={isDeleting}
                className="disabled:!border-transparent disabled:!bg-transparent"
              >
                {isDeleting ? t('common.deleting') : t('common.delete')}
              </Button>
            </div>
          )}
        </div>

        {isLoading ? (
          <DashboardStateBlock
            loading
            compact
            title={t('stockBar.loading')}
          />
        ) : items.length === 0 ? (
          <DashboardStateBlock
            title={t('stockBar.emptyTitle')}
            description={t('stockBar.emptyDescription')}
            icon={(
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
            )}
          />
        ) : (
          <div className="space-y-1.5">
            {items.map((item) => {
              const code = item.stockCode || '';
              const isMarket = isMarketReview(code);
              const isSelected = selectedRecordId !== undefined
                ? selectedRecordId === item.id
                : selectedStockCode === code;
              const isChecked = selectedRecordIds.has(item.id);

              return (
                <div key={`${code}-${item.id}`} className="flex items-start gap-2 group">
                  {onDeleteRecord && (
                    <div className="pt-5">
                      <input
                        type="checkbox"
                        checked={isChecked}
                        onChange={() => toggleRecord(item.id)}
                        disabled={isDeleting}
                        className="h-3.5 w-3.5 cursor-pointer rounded border-subtle-hover bg-transparent accent-primary focus:ring-primary/30 disabled:opacity-50"
                      />
                    </div>
                  )}
                  <StockBarItemComponent
                    item={item}
                    isViewing={isSelected}
                    onClick={onItemClick}
                    onDelete={onDeleteRecord}
                    isDeleting={isDeleting}
                    isMarketReview={isMarket}
                  />
                </div>
              );
            })}
          </div>
        )}
      </ScrollArea>
    </aside>
  );
};
