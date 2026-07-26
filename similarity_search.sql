-- ============================================================
-- Oracle Advanced Search Function with Phonetic Matching
-- ============================================================

-- DROP TYPES (if they exist) - with error handling
BEGIN
    EXECUTE IMMEDIATE 'DROP TYPE product_result_tab FORCE';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -4043 THEN  -- ORA-04043: object does not exist
            RAISE;
        END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE 'DROP TYPE product_result FORCE';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -4043 THEN
            RAISE;
        END IF;
END;
/

-- Create a result type for the function
CREATE TYPE product_result AS OBJECT (
    code VARCHAR2(50),
    description VARCHAR2(4000),
    similarity NUMBER
);
/

CREATE TYPE product_result_tab AS TABLE OF product_result;
/

-- Advanced search function with phonetic and keyword matching
CREATE OR REPLACE FUNCTION fn_advanced_search(p_termos IN VARCHAR2)
    RETURN product_result_tab PIPELINED
AS
    v_termos SYS.ODCIVARCHAR2LIST := SYS.ODCIVARCHAR2LIST();
    v_token VARCHAR2(1000);
    v_description VARCHAR2(4000);
    v_score NUMBER;
    v_dummy NUMBER;
BEGIN
    -- Split search terms
    FOR i IN 1..REGEXP_COUNT(p_termos, '\S+') LOOP
        v_termos.EXTEND;
        v_termos(i) := LOWER(REGEXP_SUBSTR(p_termos, '\S+', 1, i));
    END LOOP;

    -- Loop through products
    FOR prod IN (SELECT code, description FROM products) LOOP
        v_description := LOWER(prod.description);
        v_score := 0;

        -- Evaluate each search term
        FOR i IN 1..v_termos.COUNT LOOP
            v_token := v_termos(i);

            -- 3 points if exact match found
            IF v_description LIKE '%' || v_token || '%' THEN
                v_score := v_score + 3;
            ELSE
                -- 2 points if phonetically similar
                BEGIN
                    SELECT 1 INTO v_dummy FROM dual
                    WHERE SOUNDEX(v_token) IN (
                        SELECT SOUNDEX(REGEXP_SUBSTR(v_description, '\w+', 1, LEVEL))
                        FROM dual
                        CONNECT BY LEVEL <= REGEXP_COUNT(v_description, '\w+')
                    );
                    v_score := v_score + 2;
                EXCEPTION
                    WHEN NO_DATA_FOUND THEN NULL;
                END;

                -- 1 point if similar by edit distance
                BEGIN
                    SELECT 1 INTO v_dummy FROM dual
                    WHERE EXISTS (
                        SELECT 1
                        FROM (
                            SELECT REGEXP_SUBSTR(v_description, '\w+', 1, LEVEL) AS palavra
                            FROM dual
                            CONNECT BY LEVEL <= REGEXP_COUNT(v_description, '\w+')
                        )
                        WHERE UTL_MATCH.EDIT_DISTANCE(palavra, v_token) <= 2
                    );
                    v_score := v_score + 1;
                EXCEPTION
                    WHEN NO_DATA_FOUND THEN NULL;
                END;
            END IF;
        END LOOP;

        -- Only return if there's at least some match
        IF v_score > 0 THEN
            PIPE ROW(product_result(prod.code, prod.description, v_score));
        END IF;
    END LOOP;

    RETURN;
END fn_advanced_search;
/

-- Grant execution to PUBLIC if needed
BEGIN
    EXECUTE IMMEDIATE 'GRANT EXECUTE ON fn_advanced_search TO PUBLIC';
EXCEPTION
    WHEN OTHERS THEN
        NULL;  -- Ignore if grant fails
END;
/