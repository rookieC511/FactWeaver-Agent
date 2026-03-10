import React from 'react';
import { Loader2, CheckCircle, XCircle, Clock } from 'lucide-react';
import type { ResearchMode } from './SearchBar';

export type TaskStatus = 'PENDING' | 'STARTED' | 'SUCCESS' | 'FAILED' | null;

interface StatusMonitorProps {
    taskId: string;
    status: TaskStatus;
    detail: string;
    researchMode: ResearchMode;
    llmCostRmb?: number;
    externalCostUsd?: number;
    tavilyCredits?: number;
}

const StatusMonitor: React.FC<StatusMonitorProps> = ({
    taskId,
    status,
    detail,
    researchMode,
    llmCostRmb = 0,
    externalCostUsd = 0,
    tavilyCredits = 0,
}) => {
    if (!status) return null;

    const getStatusConfig = () => {
        switch (status) {
            case 'PENDING':
                return { icon: <Clock size={24} />, title: '研究排队中', className: 'pending', spin: false };
            case 'STARTED':
                return { icon: <Loader2 size={24} />, title: '执行中', className: 'started', spin: true };
            case 'SUCCESS':
                return { icon: <CheckCircle size={24} />, title: '研究完成', className: 'success', spin: false };
            case 'FAILED':
                return { icon: <XCircle size={24} />, title: '任务失败', className: 'error', spin: false };
            default:
                return { icon: <Loader2 size={24} />, title: '处理中', className: '', spin: true };
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
                    <span className="task-id-badge">Mode: {researchMode}</span>
                    <span className="task-id-badge">LLM: RMB {llmCostRmb.toFixed(4)}</span>
                    <span className="task-id-badge">External: USD {externalCostUsd.toFixed(4)}</span>
                    {tavilyCredits > 0 && <span className="task-id-badge">Tavily: {tavilyCredits.toFixed(1)} cr</span>}
                </div>
            </div>
        </div>
    );
};

export default StatusMonitor;
