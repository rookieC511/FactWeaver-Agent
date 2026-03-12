import React from 'react';
import { Loader2, CheckCircle, XCircle, Clock } from 'lucide-react';
import type { ResearchMode } from './SearchBar';

export type TaskStatus = 'PENDING' | 'QUEUED' | 'STARTED' | 'SUCCESS' | 'FAILED' | null;
export type PublishStatus = 'PENDING' | 'PUBLISHED' | 'FAILED' | 'LOCAL_FALLBACK' | 'CACHED' | '';

interface StatusMonitorProps {
    taskId: string;
    status: TaskStatus;
    detail: string;
    researchMode: ResearchMode;
    llmCostRmb?: number;
    externalCostUsd?: number;
    tavilyCredits?: number;
    publishStatus?: PublishStatus;
}

const StatusMonitor: React.FC<StatusMonitorProps> = ({
    taskId,
    status,
    detail,
    researchMode,
    llmCostRmb = 0,
    externalCostUsd = 0,
    tavilyCredits = 0,
    publishStatus = '',
}) => {
    if (!status) return null;

    const getStatusConfig = () => {
        switch (status) {
            case 'PENDING':
                return { icon: <Clock size={24} />, title: '\u7814\u7a76\u6392\u961f\u4e2d', className: 'pending', spin: false };
            case 'QUEUED':
                return { icon: <Clock size={24} />, title: '\u5df2\u5165\u961f\u7b49\u5f85\u6267\u884c', className: 'pending', spin: false };
            case 'STARTED':
                return { icon: <Loader2 size={24} />, title: '\u6267\u884c\u4e2d', className: 'started', spin: true };
            case 'SUCCESS':
                return { icon: <CheckCircle size={24} />, title: '\u7814\u7a76\u5b8c\u6210', className: 'success', spin: false };
            case 'FAILED':
                return { icon: <XCircle size={24} />, title: '\u4efb\u52a1\u5931\u8d25', className: 'error', spin: false };
            default:
                return { icon: <Loader2 size={24} />, title: '\u5904\u7406\u4e2d', className: '', spin: true };
        }
    };

    const config = getStatusConfig();

    return (
        <div className={`glass-panel status-panel ${config.className}`}>
            <div className={`status-icon ${config.spin ? 'spin' : ''}`}>
                {config.icon}
            </div>
            <div className="status-info">
                <h3>{config.title}</h3>
                <p>{detail}</p>
                <div style={{ marginTop: '0.5rem', display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
                    <span className="task-id-badge">ID: {taskId.split('-')[0]}...</span>
                    <span className="task-id-badge">{`\u6a21\u5f0f: ${researchMode}`}</span>
                    {publishStatus && <span className="task-id-badge">{`Publish: ${publishStatus}`}</span>}
                    <span className="task-id-badge">{`LLM: RMB ${llmCostRmb.toFixed(4)}`}</span>
                    <span className="task-id-badge">{`\u5916\u90e8: USD ${externalCostUsd.toFixed(4)}`}</span>
                    {tavilyCredits > 0 && <span className="task-id-badge">{`Tavily: ${tavilyCredits.toFixed(1)} cr`}</span>}
                </div>
            </div>
        </div>
    );
};

export default StatusMonitor;
