import React from 'react';
import { Pagination, Typography } from 'antd';
import styled from 'styled-components';
import { FacetFilterInput, FacetMetadata, SearchResults as SearchResultType } from '../../../../../../types.generated';
import { SearchFilters } from '../../../../../search/SearchFilters';
import { SearchCfg } from '../../../../../../conf';
import { EntityNameList } from '../../../../../recommendations/renderer/component/EntityNameList';

const SearchBody = styled.div`
    display: flex;
    flex-direction: row;
`;

const PaginationInfo = styled(Typography.Text)`
    padding: 0px;
`;

const FiltersContainer = styled.div`
    display: block;
    max-width: 260px;
    min-width: 260px;
    border-right: 1px solid;
    border-color: ${(props) => props.theme.styles['border-color-base']};
`;

const ResultContainer = styled.div`
    flex: 1;
    margin-bottom: 20px;
`;

const PaginationInfoContainer = styled.div`
    padding: 8px;
    padding-left: 16px;
    border-bottom: 1px solid;
    border-color: ${(props) => props.theme.styles['border-color-base']};
    display: flex;
    justify-content: space-between;
    align-items: center;
`;

const FiltersHeader = styled.div`
    font-size: 14px;
    font-weight: 600;

    padding-left: 20px;
    padding-right: 20px;
    padding-bottom: 8px;

    width: 100%;
    height: 46px;
    line-height: 46px;
    border-bottom: 1px solid;
    border-color: ${(props) => props.theme.styles['border-color-base']};
`;

const StyledPagination = styled(Pagination)`
    margin: 0px;
    padding: 0px;
`;

const SearchFilterContainer = styled.div`
    padding-top: 10px;
`;

interface Props {
    page: number;
    searchResponse?: SearchResultType | null;
    filters?: Array<FacetMetadata> | null;
    selectedFilters: Array<FacetFilterInput>;
    loading: boolean;
    showFilters?: boolean;
    onChangeFilters: (filters: Array<FacetFilterInput>) => void;
    onChangePage: (page: number) => void;
}

export const EmbeddedListSearchResults = ({
    page,
    searchResponse,
    filters,
    selectedFilters,
    loading,
    showFilters,
    onChangeFilters,
    onChangePage,
}: Props) => {
    const pageStart = searchResponse?.start || 0;
    const pageSize = searchResponse?.count || 0;
    const totalResults = searchResponse?.total || 0;
    const lastResultIndex = pageStart + pageSize > totalResults ? totalResults : pageStart + pageSize;

    const onFilterSelect = (newFilters) => {
        onChangeFilters(newFilters);
    };

    return (
        <>
            <SearchBody>
                {!!showFilters && (
                    <FiltersContainer>
                        <FiltersHeader>Filter</FiltersHeader>
                        <SearchFilterContainer>
                            <SearchFilters
                                loading={loading}
                                facets={filters || []}
                                selectedFilters={selectedFilters}
                                onFilterSelect={onFilterSelect}
                            />
                        </SearchFilterContainer>
                    </FiltersContainer>
                )}
                <ResultContainer>
                    {!loading && (
                        <>
                            <EntityNameList
                                entities={
                                    searchResponse?.searchResults?.map((searchResult) => searchResult.entity) || []
                                }
                            />
                        </>
                    )}
                    <PaginationInfoContainer>
                        <PaginationInfo>
                            <b>
                                {lastResultIndex > 0 ? (page - 1) * pageSize + 1 : 0} - {lastResultIndex}
                            </b>{' '}
                            of <b>{totalResults}</b>
                        </PaginationInfo>
                        <StyledPagination
                            current={page}
                            pageSize={SearchCfg.RESULTS_PER_PAGE}
                            total={totalResults}
                            showLessItems
                            onChange={onChangePage}
                            showSizeChanger={false}
                        />
                        <span />
                    </PaginationInfoContainer>
                </ResultContainer>
            </SearchBody>
        </>
    );
};
