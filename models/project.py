from flr import BaseModel, r, FlrException
import peewee as pw
import requests
from requests.auth import HTTPBasicAuth
import json
import pandas as pd
import xlsxwriter
from io import BytesIO, StringIO
import base64
from datetime import datetime
from pytz import timezone
import os

URL = os.environ.get("flr_api_dooblo")
USER = os.environ.get("flr_api_dooblo_key")
PASSWORD = os.environ.get("flr_api_dooblo_pas")

PROCESOS = [
    ('Múltiple', 'Múltiple'),
    ('Concatenar', 'Concatenar'),
    ('Renombrar', 'Renombrar'),
]

def divide_chunks(l, n):
    # looping till length l
    for i in range(0, len(l), n):
        yield l[i:i + n]

def utc_to_local(datetime_utc):
    return timezone("UTC").localize(datetime_utc).astimezone(timezone("America/Mexico_City"))

class Project(BaseModel):
    name = pw.CharField(verbose_name="Nombre")
    database_procesada = pw.FileField(verbose_name="Base de datos procesada", null=True)

    # Survey to go
    surveyid = pw.CharField(verbose_name="SurveyID", null=True)
    

    def get_interview_ids(self):
        if not self.surveyid:
            raise FlrException("LLene el campo SurveyID")
        method = "/SurveyInterviewIDs"
        headers = { "Content-type": "application/json"}
        params = {
            "surveyIDs": self.surveyid
        }
        try:
            response = requests.get(
                URL + method,
                auth=HTTPBasicAuth(USER, PASSWORD),
                headers=headers,
                params=params
            )
            response.raise_for_status()
            if response.status_code == 200:
                return response.json()
        except requests.exceptions.HTTPError as err:
            raise FlrException(err)

    def get_simple_export(self, interview_ids):
        method = "/SimpleExport"
        headers = { "Content-type": "application/json"}
        columns = None
        data = []
        n = 99
        for chunk_interview_ids in list(divide_chunks(interview_ids, n)):
            params = {
                "surveyID": self.surveyid,
                "subjectIDS": ",".join([str(x) for x in chunk_interview_ids]),
                "includeNulls": True,
            }
            try:
                response = requests.get(
                    URL + method,
                    auth=HTTPBasicAuth(USER, PASSWORD),
                    headers=headers,
                    params=params
                )
                response.raise_for_status()
                if response.status_code == 200:
                    datos = response.json()
                    for subject in datos["Subjects"]:
                        if columns is None:
                            columns = [x["Var"] for x in subject["Columns"]]
                        data.append({x["Var"]: x["Value"] for x in subject["Columns"]})
                else:
                    raise FlrException("Ha ocurrido en error: {}".format(response.status_code))
            except requests.exceptions.HTTPError as err:
                raise FlrException(err)
        df = pd.DataFrame(data, columns=columns)
        df.sort_values('SbjNum', inplace=True)
        df.reset_index(drop=True, inplace=True)
        df.fillna("-1", inplace=True)
        return df

    def get_edicion_base(self, df):
        df.replace(to_replace=[r"\\t|\\n|\\r", "\t|\n|\r", "_x000D_"], value=["", "", ""], regex=True, inplace=True)
        indice = []
        for conjunto in sorted(self.edicion_base_project, key=lambda x: x.id):
            if conjunto.proceso == "Múltiple":
                # Original
                ori_ini = conjunto.nombre_ori + conjunto.ini_ori
                ori_fin = conjunto.nombre_ori + conjunto.fin_ori
                ori_ini_idx = df.columns.get_loc(ori_ini)
                ori_fin_idx = df.columns.get_loc(ori_fin)
                if ori_ini_idx and ori_fin_idx:
                    frame = df.iloc[:, ori_ini_idx: ori_fin_idx + 1]
                    df = df.drop(df.columns[ori_ini_idx: ori_fin_idx + 1], axis=1)
                    datos = frame.to_dict("split")
                    maxcol = 0
                    for n, row in enumerate(datos["data"]):
                        ndata = [x for x in row if not x in {"0", "-1", 0, -1}]
                        datos["data"][n] = ndata
                        maxcol = max(maxcol, len(ndata))
                    columns = datos["columns"][:maxcol]
                    df2 = pd.DataFrame(datos["data"], columns=columns)
                    insert_index = ori_ini_idx
                    for n, column in enumerate(df2.columns, 1):
                        nsol = int(conjunto.ini_sol) + (n - 1)
                        insert_index = ori_ini_idx + (n - 1)
                        df.insert(loc=insert_index, column="{}{}".format(conjunto.nombre_sol, nsol), value=df2[column])
                        if nsol > int(conjunto.fin_sol):
                            indice.append("Se agrega una nueva columna: {}{}".format(conjunto.nombre_sol, nsol))
                    # Cuándo hay menos respuestas de el rango de lo solicitado
                    if nsol < int(conjunto.fin_sol):
                        for n, faltante in enumerate(range(nsol + 1, int(conjunto.fin_sol) + 1)):
                            df.insert(loc=insert_index + (n + 1), column="{}{}".format(conjunto.nombre_sol, faltante), value="")
                else:
                    raise FlrException("No se encontró el índice alguna de estas dos columnas", ori_ini, ori_fin)
            elif conjunto.proceso == "Concatenar":
                # Original
                ori_ini = conjunto.nombre_ori + conjunto.ini_ori
                ori_fin = conjunto.nombre_ori + conjunto.fin_ori
                ori_ini_idx = df.columns.get_loc(ori_ini)
                ori_fin_idx = df.columns.get_loc(ori_fin)
                if ori_ini_idx and ori_fin_idx:
                    frame = df.iloc[:, ori_ini_idx: ori_fin_idx + 1]
                    df = df.drop(df.columns[ori_ini_idx: ori_fin_idx + 1], axis=1)
                    datos = frame.to_dict("split")
                    for n, row in enumerate(datos["data"]):
                        datos["data"][n] = '//'.join([x for x in row if not x in {"0", "-1"}])
                    columns = datos["columns"][:1]
                    df2 = pd.DataFrame(datos["data"], columns=columns)
                    for column in df2.columns:
                        df.insert(loc=ori_ini_idx, column="{}".format(conjunto.nombre_sol), value=df2[column])
                else:
                    raise FlrException("No se encontró el índice alguna de estas dos columnas", ori_ini, ori_fin)
            elif conjunto.proceso == "Renombrar":
                df.rename(columns={conjunto.nombre_ori: conjunto.nombre_sol}, inplace=True)

        return indice, df
        
    def download_data(self):
        interview_ids = self.get_interview_ids()
        df = self.get_simple_export(interview_ids)
        indice, df = self.get_edicion_base(df)
        output = BytesIO()
        wb = xlsxwriter.Workbook(output)
        datetime_fmt = wb.add_format({'num_format': 'dd/mm/yyyy HH:MM'})
        time_fmt = wb.add_format({'num_format': '[HH]:MM:SS'})

        ws = wb.add_worksheet("Índice")
        for row, value in enumerate(indice, 0):
            ws.write(row, 0, value)

        ws = wb.add_worksheet("Base de datos")
        for col, column in enumerate(df.columns):
            column_name = df[column].name
            if column_name in {'Date', 'Upload', 'RvwTime', 'VStart', 'VEnd'}:
                ws.write_column(0, col, [column_name] + df[column].fillna('').tolist(), datetime_fmt)
            elif column_name in {'Duration'}:
                ws.write_column(0, col, [column_name] + df[column].fillna('').tolist(), time_fmt)
            else:
                ws.write_column(0, col, [column_name] + df[column].fillna('').tolist())
        wb.close()
        output.seek(0)
        datas = base64.b64encode(output.read())
        fecha_proceso = utc_to_local(datetime.now()).strftime('%Y-%m-%d')
        self.flr_update({'database_procesada': {'datab64': datas, 'name': '{}-{}-pr.xlsx'.format(self.name, fecha_proceso)}}, [('id','=',self.id)])
        return True
    
Project.r()

class EdicionBaseProject(BaseModel):
    _order = "created_on asc"
    project_id = pw.ForeignKeyField(Project, verbose_name="ediciones_base",
        backref="edicion_base_project", on_delete="CASCADE")
    proceso = pw.CharField(choices=PROCESOS, verbose_name="Proceso")
    nombre_ori = pw.CharField(verbose_name="Nombre original", null=True)
    ini_ori = pw.CharField(verbose_name="Número inicial del original", null=True)
    fin_ori = pw.CharField(verbose_name="Número final del original", null=True)
    nombre_sol = pw.CharField(verbose_name="Nombre solicitado", null=True)
    ini_sol = pw.CharField(verbose_name="Número inicial del solicitado", null=True)
    fin_sol = pw.CharField(verbose_name="Número final del solicitado", null=True)

EdicionBaseProject.r()