import React, { useState } from 'react';
import { Search } from 'lucide-react';

export type ResearchMode = 'low' | 'medium' | 'high';

interface SearchBarProps {
    onSearch: (query: string, researchMode: ResearchMode) => void;
    isLoading: boolean;
}

const MODE_OPTIONS: Array<{ value: ResearchMode; label: string; hint: string }> = [
    { value: 'low', label: 'Low', hint: '\u4f4e\u6210\u672c' },
    { value: 'medium', label: 'Medium', hint: '\u5747\u8861' },
    { value: 'high', label: 'High', hint: '\u9ad8\u8d28\u91cf' },
];

const SearchBar: React.FC<SearchBarProps> = ({ onSearch, isLoading }) => {
    const [query, setQuery] = useState('');
    const [researchMode, setResearchMode] = useState<ResearchMode>('medium');

    const handleSubmit = (e: React.FormEvent) => {
        e.preventDefault();
        if (query.trim() && !isLoading) {
            onSearch(query.trim(), researchMode);
        }
    };

    return (
        <form className="search-shell" onSubmit={handleSubmit}>
            <div className="search-container">
                <input
                    type="text"
                    className="search-input"
                    placeholder={'\u8f93\u5165\u4f60\u8981\u6df1\u5ea6\u7814\u7a76\u7684\u95ee\u9898\uff0c\u4f8b\u5982\uff1a\u5206\u6790\u7845\u57fa\u6d41\u52a8 API \u6210\u672c'}
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    disabled={isLoading}
                />
                <button type="submit" className="search-button" disabled={!query.trim() || isLoading}>
                    <Search size={24} />
                </button>
            </div>

            <div className="mode-switch" role="tablist" aria-label={'\u7814\u7a76\u6a21\u5f0f'}>
                {MODE_OPTIONS.map((mode) => (
                    <button
                        key={mode.value}
                        type="button"
                        className={`mode-chip ${researchMode === mode.value ? 'active' : ''}`}
                        onClick={() => setResearchMode(mode.value)}
                        disabled={isLoading}
                        aria-pressed={researchMode === mode.value}
                    >
                        <span>{mode.label}</span>
                        <small>{mode.hint}</small>
                    </button>
                ))}
            </div>
        </form>
    );
};

export default SearchBar;
